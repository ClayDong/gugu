# 28本股书知识系统 — Phase F 收尾计划

## Summary

28本股书知识系统的 Phase A-E 已全部落地（KB Agent、冲突裁决器、小白学堂、用户画像、Web 问答、增强通知共 6 大模块均已实现并集成）。本计划聚焦 Phase F 收尾工作：**补齐测试覆盖、运行全量回归、更新文档**。核心缺口为 `test_tutor.py` 缺失、`test_web_api.py` 未覆盖新增 QA 端点、README 未记录知识库系统。

---

## Current State Analysis

### 已完成（Phase A-E，经探索核实）
| 模块 | 文件 | 状态 |
|---|---|---|
| 用户画像 | `src/gugu/models/user_profile.py` (146行) | EXISTS |
| 概念库 | `src/gugu/wisdom/concepts.json` (50概念) | EXISTS |
| KB 索引器 | `src/gugu/wisdom/kb_indexer.py` (231行) | EXISTS |
| KB Agent | `src/gugu/wisdom/kb_agent.py` (312行) | EXISTS |
| 冲突裁决器 | `src/gugu/wisdom/conflict_resolver.py` (527行) | EXISTS |
| Advisor 集成 | `src/gugu/wisdom/advisor.py` (1020行) | EXISTS |
| 小白学堂 | `src/gugu/wisdom/tutor.py` (111行) | EXISTS |
| Web QA | `src/gugu/web/qa.py` (140行) + `qa.html` (257行) | EXISTS |
| Web 注册 | `src/gugu/web/app.py` L359-362 | EXISTS |
| 通知增强 | `formatter.py` + `feishu.py` (3新函数) | EXISTS |
| 调度器 | `scheduler.py` `concept_daily_2000` (18任务) | EXISTS |
| KB 索引数据 | `data/kb_index/` (33本, 22353块) | EXISTS |
| 构建脚本 | `scripts/build_kb_index.py` | EXISTS |
| 依赖 | `pyproject.toml` (rank-bm25, jieba, python-docx) | EXISTS |

### 现有测试覆盖
| 测试文件 | 行数 | 测试数 | 覆盖范围 |
|---|---|---|---|
| `test_kb_agent.py` | 337 | 19 | KB 检索/问答 ✅ |
| `test_conflict_resolver.py` | 344 | 23 | 冲突裁决 ✅ |
| `test_wisdom.py` | 476 | 20 | Advisor ✅ |
| `test_p0_wisdom.py` | 412 | 33 | book_router ✅ |
| `test_scheduler.py` | 113 | 7 | 含 concept_daily_2000 ✅ |
| `test_web_api.py` | 333 | 15 | 原有端点 ✅，**QA 端点未覆盖 ❌** |
| `test_tutor.py` | — | — | **缺失 ❌** |

### 缺口汇总
1. `tests/unit/test_tutor.py` 不存在 — tutor.py 4 个公开函数无测试
2. `test_web_api.py` 未覆盖 8 个新增 QA 路由（/api/qa, /api/qa/ask, /api/qa/history, /api/qa/stats, /api/concepts, /api/concepts/{id}, /api/profile GET/POST）
3. 全量测试未运行 — 无法确认 733+ 测试无回归
4. `README.md` 无"知识库系统"章节 — 架构/用法未文档化

---

## Proposed Changes

### 变更1：新建 `tests/unit/test_tutor.py`

**目标**：覆盖 `tutor.py` 的 4 个公开函数，遵循项目现有测试风格（pytest class 分组）。

**测试用例**（约 10 个）：
- `TestGetDailyConcept`:
  - `test_returns_concept_with_required_fields` — 返回 dict 含 id/name/definition
  - `test_increments_progress_index` — 连续调用 index 递增并回绕
  - `test_saves_progress_to_file` — 验证 concept_progress.json 被写入
  - `test_returns_none_when_concepts_empty` — mock 空概念库返回 None
- `TestGetConceptById`:
  - `test_returns_concept_for_valid_id` — 已知 id 返回概念
  - `test_returns_none_for_unknown_id` — 未知 id 返回 None
- `TestSearchConcepts`:
  - `test_matches_name` — 关键词匹配 name
  - `test_matches_definition` — 关键词匹配 definition
  - `test_case_insensitive` — 大写关键词也能匹配
  - `test_returns_empty_for_no_match` — 无匹配返回空列表
- `TestGetProgressStats`:
  - `test_returns_stats_dict` — 含 total_concepts/shown_count/current_index/recent_concepts

**实现要点**：
- 用 `tmp_path` 或 `monkeypatch` 隔离 `_PROGRESS_FILE`，避免污染真实 `data/concept_progress.json`
- 备份并恢复模块级 `_concepts_cache`，避免测试间污染
- 不依赖网络/LLM，纯函数测试

### 变更2：扩展 `tests/unit/test_web_api.py`

**目标**：为 8 个 QA 路由添加测试，沿用现有 TestClient + mock 模式。

**新增测试类与用例**（约 12 个）：
- `TestQAPage`:
  - `test_qa_page_returns_html` — GET /api/qa 返回 200 + HTML 含 "知识库" 字样
- `TestQAAsk`:
  - `test_ask_returns_answer` — mock `get_kb_agent().ask()` 返回 KBAnswer，POST 返回 answer+citations
  - `test_ask_empty_question_returns_400` — 空 question 返回 400
  - `test_ask_kb_failure_returns_500` — mock KB 抛异常返回 500
- `TestQAHistory`:
  - `test_history_returns_list` — GET /api/qa/history 返回 {"history": [...]}
  - `test_history_grows_after_ask` — ask 后 history 增加
- `TestQAStats`:
  - `test_stats_returns_dict` — mock `get_kb_agent().get_stats()` 返回统计
- `TestConcepts`:
  - `test_list_concepts_returns_all` — GET /api/concepts 返回 concepts 列表 + stats
  - `test_get_concept_by_id` — GET /api/concepts/安全边际 返回概念详情
  - `test_get_concept_unknown_returns_404` — 未知 id 返回 404
- `TestProfile`:
  - `test_get_profile_returns_dict` — GET /api/profile 返回画像 4 字段
  - `test_post_profile_updates` — POST /api/profile 写入并返回更新后画像

**实现要点**：
- mock `gugu.web.qa.get_kb_agent`（模块级单例），避免依赖真实索引
- mock `gugu.web.qa._qa_history` 或在测试间清理
- POST /api/profile 测试用 `tmp_path` 隔离 `data/user_profile.json`
- 沿用 `from fastapi.testclient import TestClient` + `create_app()` 模式

### 变更3：运行全量测试 + 修复回归

**步骤**：
1. 运行 `.venv/bin/python -m pytest tests/ --no-cov -q` 获取基线
2. 若有失败，定位并修复（重点关注新增模块与现有模块的集成边界）
3. 运行带覆盖率：`.venv/bin/python -m pytest tests/ --cov=gugu --cov-report=term-missing -q`
4. 确认整体覆盖率不低于 70%（项目历史基线）

**预期**：新增约 22 个测试，总数从 733 → ~755，覆盖率维持在 70%+。

### 变更4：更新 `README.md`

**位置**：在"模块说明"（L71-88）之后、"测试"（L89）之前插入新章节。

**内容**：
```markdown
## 知识库系统

gugu 集成了 33 本经典股书的知识库，为小白用户提供专业指导。

### 架构
采用混合架构：
- **28 个蒸馏视角（Skill）**：决策时快速注入专家观点
- **KB Agent（BM25 检索）**：教育问答与原文引用
- **冲突裁决器**：多视角矛盾时加权融合给出可解释决策

### 核心模块
- `src/gugu/wisdom/kb_agent.py` — 知识库问答（BM25 + jieba 中文检索）
- `src/gugu/wisdom/conflict_resolver.py` — 多维权重冲突裁决
- `src/gugu/wisdom/tutor.py` — 每日概念推送
- `src/gugu/models/user_profile.py` — 用户画像（风险/风格/经验/期限）
- `src/gugu/web/qa.py` — Web 问答 API

### 构建知识库索引
索引数据源：`/Users/dong/workspace/pdf2word/知识库_docx/`（33 本 docx）

构建命令：
bash
python scripts/build_kb_index.py --force

索引产物：`data/kb_index/`（chunks.json + tokens.pkl + meta.json）

### 使用
- Web 问答：访问 `http://127.0.0.1:8000/api/qa`
- 每日概念：每日 20:00 飞书推送一个核心概念
- 信号增强：买卖信号卡附"导师视角"栏，冲突时发"多视角研讨"卡
```

---

## Assumptions & Decisions

1. **不新增功能**：Phase F 仅做测试补齐与文档，不改动已实现的业务逻辑
2. **测试隔离**：所有涉及文件写入的测试（progress、profile）用 `tmp_path` 或 `monkeypatch` 隔离，不污染真实数据
3. **mock 策略**：Web API 测试 mock KB Agent 单例，不依赖真实索引/LLM；tutor 测试 mock 概念库加载
4. **文档精简**：README 新增章节控制在 30 行内，详细文档留在代码 docstring
5. **不重跑索引构建**：索引已存在（33本/22353块），性能已在前期验证（98秒构建，BM25 查询毫秒级）

---

## Implementation Steps

### Step 1：创建 `tests/unit/test_tutor.py`（约 120 行）
- 4 个测试类，10 个测试用例
- 隔离 `_PROGRESS_FILE` 和 `_concepts_cache`

### Step 2：扩展 `tests/unit/test_web_api.py`（追加约 180 行）
- 6 个新测试类，12 个测试用例
- mock KB Agent，隔离 profile 写入

### Step 3：运行全量测试
- `.venv/bin/python -m pytest tests/ --no-cov -q`
- 修复任何回归失败

### Step 4：运行覆盖率
- `.venv/bin/python -m pytest tests/ --cov=gugu --cov-report=term-missing -q`
- 确认 ≥70%

### Step 5：更新 `README.md`
- 在"模块说明"后插入"知识库系统"章节

---

## Verification Steps

### 测试验证
1. 新增 `test_tutor.py` 10 个测试全部 PASS
2. 扩展后 `test_web_api.py` 原有 15 + 新增 12 = 27 个测试全部 PASS
3. 全量 `pytest tests/ -q` 无 FAIL，总数 ≥ 755
4. 覆盖率 ≥ 70%

### 文档验证
1. README.md 含"知识库系统"章节
2. 章节涵盖：架构、核心模块、索引构建命令、使用方式
3. 命令 `python scripts/build_kb_index.py --force` 在文档中可复制执行

### 回归验证
1. 现有 KB Agent / 冲突裁决器 / Advisor 测试不受影响
2. 调度器 18 个任务断言通过
3. 飞书通知格式化测试通过
