# 28本股书知识系统重构计划

## Summary

基于第一性原理分析，采用**混合架构**：28个蒸馏视角（Skill）继续承担决策时的快速认知注入，新增**知识库Agent**（RAG检索34本原书）承担小白教育问答与原文引用。核心创新是**多维权重冲突裁决器**——当不同书籍对同一场景给出矛盾建议时，通过场景适配度×历史胜率×用户画像×时效性的加权融合算法给出可解释的最终决策，分散度高时触发"多视角研讨"飞书卡片。

完整版新增6个模块：KB Agent、冲突裁决器、小白学堂、用户画像、Web问答、增强通知。性能上采用本地 sentence-transformers MiniLM（90MB）+ FAISS，无外部服务依赖，适配当前电脑。

---

## Current State Analysis

### 现有资产
- **34本原书**：`/Users/dong/workspace/pdf2word/知识库_docx/`（docx，可检索）+ `/Users/dong/Desktop/股书/`（PDF，备份）
- **28个蒸馏视角**：`src/gugu/wisdom/skills/books/*/SKILL.md`，6大类（价值/成长/技术/交易系统/宏观/哲学）
- **6个决策skill**：`src/gugu/wisdom/skills/stock-*/SKILL.md`（来自《炒股的智慧》）
- **BookPerspectiveRouter**：静态+动态（波动率）路由，9场景映射
- **WisdomAdvisor**：LLM决策（async）+ fallback硬编码

### 核心缺口（来自前期分析）
1. `references/research/` 目录全空——无原书内容用于RAG
2. 路由维度单一（仅场景，无股票画像/用户画像）
3. 冲突建议无显式处理——LLM看到拼接文本自行综合，不可解释
4. `max_perspectives` 配置与实现脱节（硬编码3）
5. 无小白教育入口——决策结果不引用原书原文
6. 无用户画像——无法个性化匹配书籍风格

### 用户初衷（小白专业指导）
小白炒股的4个核心痛点：
- 不知道**为什么**买卖（缺教育）
- 看到**矛盾建议**无所适从（缺裁决）
- 学不到**系统知识**（缺问答）
- 决策**无依据**（缺引用）

---

## Proposed Changes

### 模块1：用户画像（基础设施，最先实现）

**文件**：`src/gugu/models/user_profile.py`（新建）

**作用**：所有个性化能力的底座——权重计算、概念推送、KB答案深度都依赖它。

**设计**：
```python
@dataclass
class UserProfile:
    risk_tolerance: str  # conservative | balanced | aggressive
    investment_style: str  # value | growth | trend | mixed
    experience: str  # novice | intermediate | expert
    horizon: str  # short | medium | long
    # 风格→视角偏好权重（用于冲突裁决器）
    style_weights: dict[str, float]  # {"value_investing": 1.2, "trading_system": 0.8, ...}
```

**存储**：`data/user_profile.json`（首次运行引导用户填写，或默认 balanced/mixed/novice/medium）

**视角权重映射**（对抗性审查：避免主观偏好，基于投资期限与风格的客观匹配）：
- 价值型+长期 → value_investing 1.3, trading_system 0.7
- 趋势型+短期 → trading_system 1.3, value_investing 0.7
- 成长型+中期 → growth_selecting 1.2, value_investing 0.9
- 混合型 → 全部 1.0

**接口**：`load_profile() -> UserProfile`、`save_profile(p)`、`get_style_weight(category, profile)`

---

### 模块2：知识库Agent（核心能力）

**文件**：
- `src/gugu/wisdom/kb_agent.py`（新建，主逻辑）
- `src/gugu/wisdom/kb_indexer.py`（新建，索引构建）
- `data/kb_index/`（faiss.index + chunks.json + metadata.json）

**性能考量**（用户强调电脑性能）：
- Embedding模型：`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（90MB，中文友好，CPU可跑）
- 向量库：FAISS本地索引（无需Chroma服务进程），`data/kb_index/`约5-10MB
- 索引构建：一次性，约2-3分钟（34本docx）；之后懒加载，首次查询<1秒
- 增量更新：检测docx文件mtime变更，触发重建

**索引构建流程**（`kb_indexer.py`）：
1. 遍历`/Users/dong/workspace/pdf2word/知识库_docx/*.docx`
2. 用`python-docx`提取纯文本
3. 切块：每块500字，重叠100字（保留上下文）
4. 批量embedding（batch_size=32）
5. 存FAISS索引 + chunks元数据（book_name, chunk_id, text, page_hint）

**KB Agent查询流程**（`kb_agent.py`）：
```python
class KnowledgeBaseAgent:
    async def ask(self, question: str, top_k: int = 5) -> KBAnswer:
        # 1. question → embedding
        # 2. FAISS检索top_k chunks
        # 3. 构建prompt：问题 + 检索片段 + "引用书籍名"
        # 4. LLM组织答案（async，复用WisdomAdvisor的httpx连接池）
        # 5. 返回 {answer, citations: [{book, snippet}], related_concepts}
```

**LLM prompt约束**：
- 答案必须基于检索片段，不得编造
- 每个论点附[书名]引用
- 小白模式：用通俗语言+类比；进阶模式：可直接引用术语

**接口契约**：
- `build_index(force: bool = False)` — 构建索引
- `ask(question, user_profile) -> KBAnswer` — 问答
- `search(query, top_k) -> list[Chunk]` — 纯检索（供冲突裁决器引用原文）

**依赖**：`sentence-transformers`、`faiss-cpu`、`python-docx`（需加到pyproject.toml）

---

### 模块3：冲突裁决器（核心算法创新）

**文件**：`src/gugu/wisdom/conflict_resolver.py`（新建）

**核心理念**（用户提出的权重因子方案）：每个视角不再是"文本拼接喂LLM"，而是独立产出建议，再加权融合。可解释、可调优、可学习。

**三步流程**：

**Step 1：多视角独立咨询**
```python
async def gather_perspectives(scenario, signal, profile) -> list[PerspectiveOpinion]:
    perspectives = book_router.select_perspectives_dynamic(...)
    opinions = []
    for p in perspectives:
        # 每个视角独立调LLM（并发，asyncio.gather）
        opinion = await _ask_perspective(p, scenario, signal)
        # {perspective, action, position, stop_loss, confidence, reason, citation}
        opinions.append(opinion)
    return opinions
```

**Step 2：多维权重计算**
```python
def compute_weights(opinions, scenario, profile, market_ctx) -> dict[str, float]:
    weights = {}
    for op in opinions:
        # 维度1：场景适配度（SCENARIO_MAPPING中的优先级，第1类=1.0，第2类=0.8，第3类=0.6）
        w_scenario = _scenario_fitness(op.perspective_category, scenario)
        # 维度2：历史胜率（追踪该视角建议的实盘结果，冷启动=1.0）
        w_winrate = _historical_winrate(op.perspective) or 1.0
        # 维度3：用户画像匹配（投资风格对齐度）
        w_profile = profile.get_style_weight(op.perspective_category)
        # 维度4：时效性（技术分析在高波动↑，价值投资在低波动↑）
        w_temporal = _temporal_fitness(op.perspective_category, market_volatility)
        # 综合权重（乘法融合，任一维度低则整体低）
        weights[op.perspective] = w_scenario * w_winrate * w_profile * w_temporal
    # 归一化
    total = sum(weights.values())
    return {k: v/total for k, v in weights.items()}
```

**Step 3：加权融合裁决**
```python
def resolve(opinions, weights) -> Resolution:
    # action投票：加权多数（每个视角的weight投给其action）
    action_scores = defaultdict(float)
    for op, w in zip(opinions, weights):
        action_scores[op.action] += w
    final_action = max(action_scores, key=action_scores.get)
    
    # position：加权平均
    final_position = sum(op.position * weights[i] for i, op in enumerate(opinions))
    
    # stop_loss：取最严格（防御性，对抗性审查：新手宁可错杀）
    final_stop = min(op.stop_loss for op in opinions if op.stop_loss)
    
    # 冲突检测：action分散度 = 1 - max(action_scores)/total
    dispersion = 1 - max(action_scores.values())
    is_conflict = dispersion > 0.3  # 阈值可配置
    
    return Resolution(final_action, final_position, final_stop,
                      is_conflict, opinions, weights, action_scores)
```

**冷启动策略**（对抗性审查）：
- 历史胜率初始化为1.0（不歧视新书）
- 累计≥5次建议后启用胜率权重
- 用`data/perspective_track.jsonl`记录每次建议结果

**接口**：`async resolve_conflict(scenario, signal, profile) -> Resolution`

---

### 模块4：小白学堂（教育功能）

**文件**：`src/gugu/wisdom/tutor.py`（新建）

**设计**：
- 预定义**50个核心概念库**：`src/gugu/wisdom/concepts.json`
  - 止损铁律、安全边际、关键点、分层下注、移动止损、最小阻力线、市场先生、能力圈、盈亏比、R乘数...
- 每个概念关联：1-2本代表书 + 对应视角 + KB检索query
- 每日20:00推送一张概念卡（飞书定时任务）

**概念卡内容**：
- 概念名 + 一句话定义
- 详细解释（KB Agent检索原书生成，附引用）
- 实战要点（3条可操作规则）
- 延伸思考（引导用户提问）

**推送调度**：`scheduler.py`新增`concept_daily_2000`任务

---

### 模块5：增强通知（用户触达）

**文件**：`src/gugu/notifier/formatter.py`（修改）+ `src/gugu/notifier/feishu.py`（修改）

**变更1：信号卡增加"导师视角"栏**
- 位置：信号理由之后
- 内容：top-2视角名 + 各自一句结论 + 原文引用片段（KB检索）+ 权重
- 示例：
  ```
  📚 导师视角
  • 利弗莫尔（权重0.32）：顺势加仓，关键点已确认 [股票大作手回忆录]
  • 陈江挺（权重0.28）：试仓20%，止损铁律 [炒股的智慧]
  ```

**变更2：冲突时发送"多视角研讨"卡**
- 触发条件：`Resolution.is_conflict == True`
- 卡片格式：每个视角一行（结论+理由+权重），底部加权裁决结果
- 颜色：黄色（warning，提示分歧）

**变更3：每日概念卡**
- 20:00推送，绿色（info）
- 折叠式：标题+定义，点击展开详细解释+引用

**对抗性审查**：避免信息过载——导师视角栏最多2个视角，原文片段限80字，冲突卡限4个视角。

---

### 模块6：Web问答页面

**文件**：
- `src/gugu/web/qa.py`（新建，API路由）
- `src/gugu/web/static/qa.html`（新建，问答页面）
- `src/gugu/web/app.py`（修改，注册路由）

**API**：
- `POST /api/qa` — `{question}` → `{answer, citations, related}`
- `GET /api/qa/history` — 最近20条问答
- `POST /api/profile` — 更新用户画像

**前端**：极简单页（输入框 + 答案区 + 引用折叠 + 历史），无框架依赖（原生JS+fetch），避免性能负担。

---

## Assumptions & Decisions

### 架构决策
1. **混合而非二选一**：Skill管"怎么决策"（快、结构化），KB管"为什么+原文"（深、可引用）。两者通过冲突裁决器协同。
2. **权重融合而非LLM独裁**：用户明确提出权重方案。多维权重可解释、可调优、可学习，比纯LLM黑箱更专业。
3. **本地embedding而非云端API**：性能+隐私+离线可用。MiniLM 90MB对当前电脑无压力。
4. **FAISS而非Chroma**：FAISS是库，无需服务进程；Chroma需起服务，增加运维负担。

### 对抗性审查要点
1. **冷启动问题**：历史胜率权重刈1.0，避免新书被歧视；≥5次后启用
2. **LLM调用并发**：多视角独立咨询用`asyncio.gather`，3个视角并发≈1次LLM延迟
3. **索引构建阻塞**：首次构建走子进程或后台任务，不阻塞引擎启动
4. **信息过载**：飞书卡片严格限流（2视角/80字引用/4冲突视角）
5. **冲突阈值**：dispersion>0.3触发冲突卡，可配置，避免过度告警
6. **权重退化**：若所有视角权重相同（冷启动），退化为等权平均，仍可用

### 不做的事（避免过度工程）
- 不引入Chroma/Qdrant等向量数据库服务
- 不做实时权重学习（用离线批次更新即可）
- 不做语音问答
- 不重写28个SKILL.md（保留现有蒸馏成果）
- 不做书籍全文翻译/重写

---

## Implementation Steps（建议顺序）

### Phase A：基础设施（1-2天）
1. 新建`src/gugu/models/user_profile.py` + `data/user_profile.json`默认配置
2. 新建`src/gugu/wisdom/concepts.json`（50个核心概念）
3. 添加依赖到`pyproject.toml`：`sentence-transformers`、`faiss-cpu`、`python-docx`

### Phase B：KB Agent（2-3天）
4. 新建`src/gugu/wisdom/kb_indexer.py`——实现`build_index()`
5. 新建`src/gugu/wisdom/kb_agent.py`——实现`ask()`、`search()`
6. 编写索引构建脚本`scripts/build_kb_index.py`
7. 单元测试`tests/unit/test_kb_agent.py`

### Phase C：冲突裁决器（2天）
8. 新建`src/gugu/wisdom/conflict_resolver.py`
9. 集成到`WisdomAdvisor._advise_llm_async`——替换现有拼接式book_context
10. 新建`data/perspective_track.jsonl`记录建议结果
11. 单元测试`tests/unit/test_conflict_resolver.py`

### Phase D：增强通知（1天）
12. 修改`formatter.py`——信号卡增加"导师视角"栏
13. 新增冲突卡格式化函数
14. 修改`scheduler.py`——新增`concept_daily_2000`任务
15. 修改`feishu.py`——发送冲突卡、概念卡

### Phase E：小白学堂 + Web（1-2天）
16. 新建`src/gugu/wisdom/tutor.py`——每日概念生成
17. 新建`src/gugu/web/qa.py` + `qa.html`——问答页面
18. 修改`src/gugu/web/app.py`——注册QA路由

### Phase F：测试与集成（1天）
19. 集成测试：完整流程（信号→多视角→冲突裁决→增强通知）
20. 性能测试：索引构建耗时、单次查询延迟、LLM并发数
21. 文档更新：`README.md`新增"知识库系统"章节

**预估总工期**：8-10天（可分阶段交付，Phase B/C为核心价值）

---

## Verification Steps

### 功能验证
1. **KB Agent**：手动提问"格雷厄姆怎么看止损"，验证答案含《聪明的投资者》引用片段
2. **冲突裁决**：构造Graham（filter）vs Livermore（buy）的冲突信号，验证加权裁决结果合理
3. **增强通知**：发送测试信号卡，确认"导师视角"栏显示且原文引用准确
4. **每日概念**：手动触发概念卡推送，验证内容完整+引用来源
5. **Web问答**：浏览器访问`http://127.0.0.1:8000/qa`，提问验证响应

### 性能验证
1. 索引构建：`time python scripts/build_kb_index.py` 应 < 5分钟
2. 单次查询：KB问答延迟应 < 3秒（含LLM）
3. 冲突裁决：3视角并发应 < 5秒
4. 内存占用：embedding模型常驻 < 500MB

### 对抗性验证
1. 冷启动：清空`data/perspective_track.jsonl`，验证权重退化为等权
2. LLM失败：mock LLM超时，验证冲突裁决器降级到fallback
3. 索引缺失：删除`data/kb_index/`，验证KB Agent优雅降级（仅用28视角）
4. 冲突阈值：调整`dispersion_threshold`，验证告警频率可控

### 回归验证
1. 现有733个单元测试全部通过
2. 飞书通知所有11种类型格式正常
3. 调度器17个任务+1个新任务（concept_daily_2000）=18个
4. Web仪表盘原有功能不受影响
