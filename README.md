# gugu

**自动化 A 股信号监控系统** — 基于多视角分析、规则引擎、三级风控和交易智慧决策层。

[![CI](https://github.com/dong4j/gugu/actions/workflows/ci.yml/badge.svg)](https://github.com/dong4j/gugu/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-69%25-brightgreen.svg)]()

---

## 核心特性

### 🧠 认知引擎
- **十大视角分析系统** — 10 位投资大师视角（巴菲特/芒格/索罗斯/林奇/利弗莫尔/邱国鹭/陈江挺/技术分析/投资习惯/大师兄）量化为可计算评分（0-10），加权综合+木桶效应判定，08:30 飞书日报推送
- **四阶段判断器** — 基于《炒股的智慧》牛皮/升势/疯狂/最后四阶段识别，MA 斜率使用线性回归（替代双点差分），抗噪声能力更强
- **五大危险信号检测** — 放量下跌/高位十字星/衰竭缺口/天量天价/连续背离
- **向下摊平检查** — 禁止在亏损仓位上加仓
- **多周期趋势确认** — 周线/日线趋势一致性检查，防止逆势操作

### 📊 策略体系
- **多策略信号融合** — 16 个注册策略（趋势/均值回归/突破/主力行为/ML 多因子），支持 weighted/majority/any 融合模式
- **当前启用策略** — `box_breakout`（箱体突破）+ `double_yin`（双阴钱袋子），按市场状态分流启用
- **ML 多因子策略** — `ml_factor`：LightGBM + PCA 正交化（窗口内拟合消除前视偏差），Walk-Forward 训练，用 26 个 Alpha 因子预测涨跌方向
- **因子 IC 分析** — `FactorICAnalyzer` 计算因子时序 IC/IR，筛选 |IC|>0.03 且 |IR|>0.3 的有效因子
- **策略准入门禁** — `scripts/validate_strategy_gate.py` 统一 9 项门槛（交易数/收益/夏普/回撤/盈利比/参数稳定性/paper 信号/paper 收益），结构化判断是否可启用，`--notify` 推送飞书验证报告
- **策略注册中心** — 26 个策略统一登记到 `strategy_manifest.yaml`，启用策略必须包含 `validation_gate` 结构化验证记录
- **市场状态自适应** — 4 类策略分类（趋势/均值回归/突破/主力行为），5 种市场状态动态调整权重乘数
- **Walk-Forward 参数优化** — 滚动窗口网格搜索 + 多股聚合训练，4 项解锁条件门控自动写回
- **自优化调度闭环** — 周日 03:00 自动跑多股聚合 Walk-Forward，6 只行业代表股，失败不污染生产参数

### 🛡️ 风控体系
- **三级风控** — L1 单股仓位上限（≤30%）、L2 日亏熔断（3%预警/5%熔断，持久化到磁盘）、L3 涨跌停+停牌+T+1
- **组合回撤熔断** — 从高点回撤 ≥15% 自动熔断，仅允许减仓
- **持仓时间风控** — 30 天未盈利警告减半仓，60 天未盈利强制清仓
- **持仓相关性监控** — 检查持仓间高相关性风险（阈值 0.85，皮尔逊相关系数）
- **熔断状态持久化** — 进程重启后自动恢复熔断状态，防止绕过铁律

### 📈 信号处理
- **信号过滤流水线** — 8 步过滤链（四阶段→危险信号→量价背离→基本面→资金流→板块轮动→行业约束→向下摊平→四阶段入场过滤→Wisdom 决策）
- **被过滤信号摘要** — 收盘报告展示当日被过滤的信号及原因，解决"沉默拒绝"问题
- **信号去重机制** — 持久化已通知信号记录（7 天窗口），引擎重启后自动恢复
- **信号告警疲劳控制** — 可配置同股最小通知间隔/单股每日信号上限/同方向批量聚合
- **信号绩效追踪** — 追踪信号产生后 1/3/5/10 日实际收益，按策略分组统计胜率

### 💰 资金流分析
- **双通道冗余采集** — akshare（主）+ 东方财富 push API 直连（降级），UA 池轮换防限流
- **大盘/行业/个股三级分析** — 主力净流入、北向资金、行业轮动强度、背离预警
- **量价推算 fallback** — 资金流 API 不可用时用量价关系推算市场方向
- **背离检测** — 主力流入+下跌（底背离）/ 主力流出+上涨（顶背离），阈值可配置

### 📡 数据层
- **三级数据源冗余** — sina（主）+ akshare（降级）+ eastmoney（兜底），主源失败自动并发降级
- **三级缓存** — Parquet 持久化缓存 + 内存 LRU 缓存 + API 实时请求
- **数据质量校验** — 实时价格校验、单位转换、字段名统一、异常值过滤

### 🔔 通知系统
- **飞书卡片消息** — 信号通知、日报、风控告警、回测报告、基金监控、宏观简报、资金流日报
- **通知重试队列** — 持久化到 JSONL，指数退避重试（1min→2min→4min→8min），防飞书限流
- **signal_only 免责声明** — 仅通知模式卡片标注"不构成交易建议"

### 📚 知识库
- **28 本经典股书蒸馏视角** — 利弗莫尔/格雷厄姆/芒格/索罗斯等大师认知框架
- **KB Agent** — BM25 + jieba 检索，33 本书 22353 个文本块
- **冲突裁决器** — 多视角矛盾时加权融合算法给出可解释决策
- **小白学堂** — 50 个核心概念，每日 20:00 飞书推送

### 🔧 技术优化
- **并发预取** — 批量预取股票历史数据使用 `asyncio.gather` 并发，大幅缩短扫描时间
- **StockRanker 并发评分** — 多只股票评分使用 Semaphore 限制并发，避免串行 I/O 阻塞
- **技术指标统一计算** — `technical.py` 集中提供 ATR/RSI/MACD/布林带/OBV，消除重复实现
- **AlphaFactory 26 因子全覆盖** — 趋势(7)+动量(7)+波动(4)+成交量(4)+形态(4) 共 26 个技术因子，含 CCI/MFI/OBV 背离/VWAP 偏离/ADX/DI 交叉/跳空缺口/十字星/锤子线/三连阳阴，综合评分直接接入 StockRanker，OBV 使用 numpy 向量化（10x 性能提升）
- **Kelly 资金管理** — 基于历史胜率+盈亏比的最优仓位计算，含金字塔加仓策略
- **仓位总控** — 综合市场状态+账户风险+持仓集中度动态计算仓位预算
- **Sharpe Ratio 含无风险利率** — 基于 2% 无风险利率（中国一年期存款利率近似）计算风险调整后收益
- **Calmar/Sortino 比率** — 回测报告新增 Calmar 比率（年化/最大回撤）和 Sortino 比率（下行波动率），更全面的风险调整评估
- **状态原子写入** — heartbeat / take_profit_state / risk_state 使用原子写入防崩溃截断
- **策略参数热加载** — 修改 `settings.yaml` 后无需重启，scheduler 每 5 分钟自动检测并重载配置
- **Walk-Forward 参数优化** — 滚动窗口网格搜索 + Calmar 比率目标函数（年化收益/最大回撤），多股聚合训练
- **数据源健康监控** — Web 仪表盘 `/api/health` 端点返回三通道数据源状态 + 资金流状态
- **调度任务可配置** — `settings.yaml` 中可单独关闭不需要的推送（宏观/视角/信号/基金等 20 个开关）
- **盘中扫描频率自适应** — 熊市/暴跌市自动跳过盘中扫描，仅保留开盘和尾盘

## 快速开始

```bash
# 安装依赖
uv sync

# 复制配置文件
cp .env.example .env
# 编辑 .env 配置飞书、LLM 等密钥

# 运行模拟盘
python scripts/run_paper.py

# 运行回测
python scripts/run_backtest.py --symbol 600519 --strategy turtle --days 120

# 运行 Web 仪表盘
python scripts/run_web.py

# 参数优化
python scripts/optimize_params.py --symbol 600519 --days 500 --write

# 信号监控模式
python scripts/run_live.py --signal-only

# 守护模式（带 watchdog 自动重启）
python scripts/quickstart.py --daemon

# 守护模式（不启用 watchdog）
python scripts/quickstart.py --daemon --no-watch

# 查看守护进程状态
python scripts/quickstart.py --status

# 停止守护进程
python scripts/quickstart.py --stop

# 策略注册中心
python scripts/verify_strategies.py list        # 列出全量策略
python scripts/verify_strategies.py check       # 代码与清单一致性校验
python scripts/verify_strategies.py dashboard   # 推送策略看板到飞书
python scripts/verify_strategies.py source "炒股的智慧"  # 按出处查询
python scripts/verify_strategies.py params trailing_stop  # 查看策略参数

# 已启用策略回测验证
python scripts/verify_enabled_strategies.py                    # 验证 box_breakout + double_yin
python scripts/verify_enabled_strategies.py --max-stocks 50    # 限制测试股票数
python scripts/verify_enabled_strategies.py --days 200         # 自定义回测天数
python scripts/verify_enabled_strategies.py --output result.json  # 输出到 JSON

# 策略准入门禁验证
python scripts/validate_strategy_gate.py --strategy box_breakout --metrics metrics.json --notify
python scripts/validate_strategy_gate.py                        # 查看当前启用策略
```

## 系统架构

```
数据层 → 策略层 → 规则引擎 → 过滤层 → 决策层 → 移动止损 → 引擎层 → 风控层 → 执行层 → 通知层
```

详见 [TECH.md](docs/TECH.md) 技术文档。

## 模块说明

| 模块 | 目录 | 说明 |
|------|------|------|
| 配置 | `config/` | 强类型 Pydantic 配置 + YAML |
| 数据 | `data/` | 多源采集、三层缓存、数据质量校验 |
| 策略 | `strategies/` | 15 个注册策略 + LLM 生成器 + 策略注册中心 |
| 回测 | `backtest/` | 含认知引擎/Walk-Forward 优化的回测引擎 |
| 风控 | `risk/` | 三级风控、组合相关性监控、回撤熔断 |
| 执行 | `execution/` | 模拟盘 + QMT 实盘骨架 |
| 引擎 | `engine/` | 主循环、信号流水线、事件驱动 |
| 过滤 | `filters/` | 基本面、资金流、行业约束、市场状态 |
| 分析 | `analysis/` | 移动止损、动态止盈、四阶段、危险信号、归因分析 |
| 决策 | `wisdom/` | 28本书视角、KB Agent、冲突裁决器、LLM/fallback 决策 |
| 通知 | `notifier/` | 飞书卡片消息 |
| 宏观 | `macro/` | 五维宏观数据采集与飞书简报 |
| Web | `web/` | FastAPI 仪表盘 + 知识库问答 |

## 知识库系统

gugu 集成了 33 本经典股书的知识库，为小白用户提供专业指导。采用混合架构：

- **28 个蒸馏视角（Skill）**：决策时快速注入专家观点（利弗莫尔、格雷厄姆、芒格等）
- **KB Agent（BM25 + jieba 检索）**：教育问答与原文引用，33 本书 22353 个文本块
- **冲突裁决器**：多视角矛盾时通过场景适配度 × 历史胜率 × 用户画像 × 时效性的加权融合算法给出可解释决策

### 核心模块

| 模块 | 文件 | 说明 |
|------|------|------|
| KB Agent | `src/gugu/wisdom/kb_agent.py` | 知识库问答（BM25 检索 + LLM 组织答案） |
| 冲突裁决器 | `src/gugu/wisdom/conflict_resolver.py` | 多维权重融合，冲突检测与研讨卡 |
| 小白学堂 | `src/gugu/wisdom/tutor.py` | 50 个核心概念，每日 20:00 推送 |
| 用户画像 | `src/gugu/models/user_profile.py` | 风险/风格/经验/期限四维画像 |
| Web 问答 | `src/gugu/web/qa.py` | RESTful API + 单页问答界面 |

### 构建知识库索引

索引数据源：`/Users/dong/workspace/pdf2word/知识库_docx/`（33 本 docx）

```bash
python scripts/build_kb_index.py --force
```

索引产物：`data/kb_index/`（chunks.json + tokens.pkl + meta.json），构建约 98 秒。

### 使用方式

- **Web 问答**：访问 `http://127.0.0.1:8000/api/qa`，提问获取带引用的专业解答
- **每日概念**：每日 20:00 飞书推送一个核心概念（安全边际、止损铁律等）
- **信号增强**：买卖信号卡附"导师视角"栏，冲突时发"多视角研讨"黄色卡片

## 测试

**1537 测试通过，5 跳过，69% 覆盖率**（当前本机 Python 3.14 验证结果）。

```bash
# 运行全部测试
uv run pytest tests/ -v --cov=src/gugu --cov-report=term-missing

# 运行单元测试
uv run pytest tests/unit/ -v

# 运行集成测试
uv run pytest tests/integration/ -v

# 运行特定模块测试
uv run pytest tests/unit/test_signal_pipeline.py -v   # 信号过滤流水线
uv run pytest tests/unit/test_perspectives.py -v       # 十大视角评分
uv run pytest tests/unit/test_money_management.py -v   # Kelly 资金管理
uv run pytest tests/unit/test_position_controller.py -v # 仓位总控
uv run pytest tests/unit/test_technical.py -v          # 技术指标
uv run pytest tests/unit/test_scheduler.py -v          # 调度器
```

## 环境要求

- Python 3.12+（3.14 已验证）
- uv（推荐）或 pip
- 飞书开放平台应用（用于通知）
- （可选）LLM API Key（用于 Wisdom 决策）
- （可选）QMT 客户端（用于实盘）

## 许可证

[MIT](LICENSE)

## 贡献

欢迎贡献！请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
