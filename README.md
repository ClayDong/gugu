# gugu

**自动化 A 股交易系统** — 基于规则引擎、多策略信号融合、三级风控和交易智慧决策层。

[![CI](https://github.com/dong4j/gugu/actions/workflows/ci.yml/badge.svg)](https://github.com/dong4j/gugu/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 核心特性

- **多策略信号融合** — 8 个内置策略（趋势/均值回归/突破），支持 weighted/majority/any 融合模式
- **三级风控体系** — L1 单股仓位上限、L2 日亏熔断（持久化）、L3 涨跌停+停牌+T+1
- **移动止损引擎** — 基于《炒股的智慧》浪谷递进算法，ATR 自适应初始止损
- **交易智慧决策层** — 28 本书籍视角多维度决策支持，LLM 增强或硬编码规则 fallback
- **认知引擎** — 四阶段判断、五大危险信号检测、向下摊平检查、多周期趋势确认
- **Walk-Forward 参数优化** — 滚动窗口网格搜索，自动写回最优参数
- **三级缓存** — Parquet 持久化缓存 + 内存 LRU 缓存 + API 实时请求
- **数据源降级** — 主源（sina）连续失败自动降级到备选源（akshare），冷却后自动切回
- **飞书通知** — 信号卡片、日报、风控告警、回测报告、基金监控
- **Web 仪表盘** — FastAPI + 持仓/信号/风险/净值历史可视化
- **盈亏归因分析** — 消费 signals_history.jsonl，生成按过滤步骤/策略/Wisdom 决策的归因报告

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
| 策略 | `strategies/` | 8 个内置策略 + LLM 生成器 |
| 回测 | `backtest/` | 含认知引擎/Walk-Forward 优化的回测引擎 |
| 风控 | `risk/` | 三级风控、组合相关性监控、回撤熔断 |
| 执行 | `execution/` | 模拟盘 + QMT 实盘骨架 |
| 引擎 | `engine/` | 主循环、信号流水线、事件驱动 |
| 过滤 | `filters/` | 基本面、资金流、行业约束、市场状态 |
| 分析 | `analysis/` | 移动止损、四阶段、危险信号、归因分析 |
| 决策 | `wisdom/` | 28本书视角、LLM/fallback 决策 |
| 通知 | `notifier/` | 飞书卡片消息 |
| 宏观 | `macro/` | 五维宏观数据采集与飞书简报 |
| Web | `web/` | FastAPI 仪表盘 |

## 测试

550+ 测试，67% 覆盖率。

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
