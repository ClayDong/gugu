# gugu

**自动化 A 股交易系统** — 基于规则引擎、多策略信号融合、三级风控和交易智慧决策层。

[![CI](https://github.com/dong4j/gugu/actions/workflows/ci.yml/badge.svg)](https://github.com/dong4j/gugu/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 核心特性

- **多策略信号融合** — 8 个内置策略（趋势/均值回归/突破），支持 weighted/majority/any 融合模式
- **策略注册中心** — 26 个策略（信号生成/风控择时/止盈止损/Skill/动态生成器）统一登记到 `strategy_manifest.yaml`，支持清单/校验/看板/出处/参数查询
- **财叔动态止盈减仓** — +30% 减 1/3 → 每 +15% 减 10% → 大跌减半，与移动止损互补（止损保本金、止盈锁利润），飞书卡片标注出处
- **信号去重机制** — 持久化已通知信号记录，引擎重启后自动恢复，避免重复推送
- **三级风控体系** — L1 单股仓位上限、L2 日亏熔断（持久化）、L3 涨跌停+停牌+T+1
- **移动止损引擎** — 基于《炒股的智慧》浪谷递进算法，ATR 自适应初始止损
- **交易智慧决策层** — 28 本书籍视角多维度决策支持，LLM 增强或硬编码规则 fallback
- **认知引擎** — 四阶段判断、五大危险信号检测、向下摊平检查、多周期趋势确认
- **Walk-Forward 参数优化** — 滚动窗口网格搜索 + 多股聚合训练，4 项解锁条件门控自动写回
- **自优化调度闭环** — 周日 03:00 自动跑多股聚合 Walk-Forward，6 只行业代表股，失败不污染生产参数
- **三级缓存** — Parquet 持久化缓存 + 内存 LRU 缓存 + API 实时请求
- **数据源三级冗余** — sina（主）+ akshare（降级）+ eastmoney（兜底，httpx 直连 push2），主源失败自动并发降级，双源失败抛异常触发降级
- **大市数据三通道** — akshare → 新浪K线 JSON → 东方财富 push2，逐级降级确保选股可用
- **飞书通知** — 信号卡片、日报、风控告警、回测报告、基金监控、宏观简报（含大师兄解读）、动态止盈减仓、策略全景看板
- **Web 仪表盘** — FastAPI + 持仓/信号/风险/净值历史可视化
- **盈亏归因分析** — 消费 signals_history.jsonl，生成按过滤步骤/策略/Wisdom 决策的归因报告
- **状态原子写入** — heartbeat / take_profit_state 改用 atomic_write_text，防崩溃截断
- **测试隔离** — 4 个 monkeypatch 重定向到 tmp_path，防污染生产文件
- **缩量回踩低吸选股** — 尾盘选股器从"涨幅追涨"改为"缩量回踩 MA20+前 5 日强势"低吸逻辑
- **移动止损抗诱多** — 去掉递减逻辑，仅创新高重置跌破计数，放量跌破浪谷直接 EXIT
- **双均线假信号过滤** — 金叉需量比 ≥1.5 + MA20>MA60 + MA20 斜率 >0（可配置关闭）
- **资金流默认拒绝** — 数据缺失时 paper/live 模式默认拒绝，仅 signal_only 放行
- **持仓时间风控** — 30 天未盈利警告减半仓，60 天未盈利强制清仓
- **多指数大市过滤** — 沪深 300 + 中证 500 + 创业板指三重验证，防止中小盘误判
- **周复盘推送** — 周日 20:00 汇总本周信号统计推送飞书
- **配置一致性** — settings.yaml 与 manifest.yaml CI 自动校验，防止策略清单分裂

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
| 策略 | `strategies/` | 8 个内置策略 + LLM 生成器 + 策略注册中心 |
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

960 测试，64% 覆盖率。

```bash
# 运行全部测试
uv run pytest tests/ -v --cov=src/gugu --cov-report=term-missing

# 运行单元测试
uv run pytest tests/unit/ -v

# 运行集成测试
uv run pytest tests/integration/ -v
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
