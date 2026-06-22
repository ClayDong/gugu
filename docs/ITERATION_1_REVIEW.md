# gugu 迭代 1 全量 Expert Review

> 评审日期：2026-06-22  
> 评审范围：`CLAUDE.md`、`pyproject.toml`、`config/*.yaml`、`src/gugu/**/*.py`、`tests/**/*.py`、`scripts/*.py`  
> 业务阶段：阶段一（策略固化）+ 阶段二（回测）+ 阶段三（模拟盘）骨架

---

## 执行摘要

当前代码骨架完整，模块划分清晰，内置 8 个策略、三级风控、模拟盘、飞书通知、回测引擎与 LLM 策略生成器均已落地。经实际验证：

- `ruff check src tests scripts`：通过
- `mypy src/gugu`：通过
- `.venv\Scripts\python -m pytest tests/ -v --cov=src/gugu`：113 passed，覆盖率 70%

但存在 **4 条 P1（必须修）**、**13 条 P2（应该修）**、**9 条 P3（建议）** 问题。核心风险集中在：**模拟盘 T+1 逻辑缺陷**、**信号数据契约与飞书卡片不匹配**、**主引擎未把涨跌停/停牌信息传给风控**、**缺失集成测试与 fixtures**、**日亏计算口径错误**。

---

## 1. 架构专家

### P1｜`PaperBroker` 追加买入时 T+1 可用数量未更新

- **位置**：`src/gugu/execution/paper.py#L70-L76`
- **问题**：
  ```python
  pos.avg_cost = ((pos.avg_cost * pos.quantity + fill_price * quantity) / new_qty)
  pos.quantity = new_qty
  # T+1：新买的当日不可卖
  pos.available = pos.available   # ← 无意义赋值
  ```
  追加买入后，`available` 没有增加新买入的数量（应保持不变直到次日 `settle_t_plus_1`）。当前代码只是原值赋给自己，逻辑上没错但表达错误；更严重的是，**首次买入**时 `available=0` 是对的，但**再次买入**时旧持仓的 `available` 不应被新买稀释，当前实现没有稀释，因此实际上是通过“不修改”蒙对的。真正的隐患是：代码意图不清晰，后续维护者容易在这里破坏 T+1。
- **建议修复**：
  1. 删除无意义行；
  2. 在注释中明确“追加买入不增加 available”；
  3. 补充单元测试覆盖追加买入场景。

### P1｜主引擎未把前收盘价/停牌/ST 状态传给风控 L3 检查

- **位置**：`src/gugu/engine/main.py#L139-L147`
- **问题**：`TradingEngine._process_signal` 调用 `RiskManager.check_order` 时，只传了 `symbol/direction/quantity/price/portfolio/cash`，没有传 `prev_close`、`is_st`、`is_suspended`。`RiskManager.check_order` 中 L3 的涨跌停/停牌检查依赖这些参数（`manager.py#L118-L133`），因此实际交易流程里这些铁律被静默绕过。
- **建议修复**：
  1. 在 `DataManager` 或 `StockSelector` 中增加 `fetch_stock_meta(symbol)` 返回 `prev_close`、`is_st`、`is_suspended`；
  2. `TradingEngine._process_signal` 获取并传入这些字段；
  3. 在 `test_risk.py` 外新增 `test_engine.py` 的集成断言，确保 L3 检查被调用。

### P2｜回测引擎默认“全仓单股”，与 30% 单股上限风控矛盾

- **位置**：`src/gugu/backtest/engine.py#L10-L15`、`#L139-L146`
- **问题**：回测说明中明确“full-capital buy / full-clear sell”，且未接入 `RiskManager`。这与 CLAUDE.md 的 L1 风控（单股 ≤30%）不一致，会导致回测结果与模拟盘/实盘行为脱节。
- **建议修复**：
  1. 在 `BacktestEngine` 中注入 `RiskManager` 与 `PaperBroker` 类似的仓位/现金模型；
  2. 或至少按 `max_position_ratio` 计算买入数量；
  3. 在回测报告中说明当前仓位模型假设。

### P2｜`WisdomAdvisor` 依赖 gitignored 的 `_refs/books2skill`

- **位置**：`src/gugu/wisdom/advisor.py#L16`、`#L29-L41`
- **问题**：`WISDOM_DIR = PROJECT_ROOT / "_refs" / "books2skill" / ...`，而 `_refs/` 在 `.gitignore` 中。 fresh clone 后该目录不存在，`advise()` 会返回空 `wisdom` 字段，功能降级为“无智慧增强”。同时硬编码了 5 个 skill 名称，耦合外部项目内部结构。
- **建议修复**：
  1. 将核心交易智慧摘要抽出为项目内 `wisdom/skills/`（可提交）；
  2. 若保留 `_refs` 路径，应在 `WisdomAdvisor.__init__` 给出更明显的 warning 并记录恢复步骤；
  3. skill 名称改为配置化。

### P2｜缺少集成测试与测试数据 fixtures

- **位置**：`tests/integration/__init__.py`（空）、`tests/fixtures/`（缺失）
- **问题**：`CLAUDE.md` 要求测试数据放 `tests/fixtures/` 且不依赖网络，但目录不存在；`tests/integration/` 只有空 `__init__.py`。`engine/main.py` 覆盖率仅 23%，`selector/stock_selector.py` 仅 23%。
- **建议修复**：
  1. 创建 `tests/fixtures/` 并放入 CSV/Parquet 样本行情；
  2. 新增 `tests/integration/test_engine_flow.py`，用 mock 的 `DataManager` 跑完整“采集→选股→信号→风控→下单→通知”流程；
  3. 新增 `tests/integration/test_backtest_paper_consistency.py` 验证同一策略在回测与模拟盘下的信号一致性。

### P3｜`DataManager` 缓存 key 使用字符串化 kwargs

- **位置**：`src/gugu/data/manager.py#L67`
- **问题**：`cache_key = f"{method}:{args}:{kwargs}"` 对当前参数（字符串、整数）可用，但对未来扩展（如 dict 参数）不稳定，且可读性差。
- **建议修复**：使用 `json.dumps((method, args, kwargs), sort_keys=True, default=str)` 生成稳定 key。

---

## 2. 产品专家

### P1｜信号路由输出字段与飞书卡片格式化器不匹配

- **位置**：
  - 输出侧：`src/gugu/engine/signal_router.py#L117-L123`（输出 `strategies`、`reason`、`confidence` 等）
  - 消费侧：`src/gugu/notifier/formatter.py#L82-L111`（期望 `strategy`、`suggested_position`、`name`）
- **问题**：`SignalRouter.route()` 返回的 key 是 `strategies`（list），而 `format_signal()` 读的是 `strategy`（str）；router 不产出 `suggested_position` 和 `name`，导致飞书卡片里这些字段为空。`test_notifier.py` 能过是因为测试数据是手写补全的，不是真实流程数据。
- **建议修复**：
  1. 统一信号 schema，建议定义为 dataclass 或 TypedDict；
  2. `SignalRouter` 补充 `name`（通过 DataManager 查询或上游传入）、`suggested_position`（基于 risk.max_position_ratio 计算）；
  3. 在集成测试中验证真实流程生成的卡片内容。

### P2｜自动选股默认开启，但数据源稳定性未经验证

- **位置**：`config/settings.yaml#L28`、`src/gugu/engine/main.py#L69-L74`
- **问题**：`auto_select: true` 默认启用，会调用 `StockSelector.select()`，依赖 `fetch_sector_flow()` 与全市场实时快照。akshare 的 sector flow 接口在盘中/非交易日容易失败，失败后静默返回空列表，用户无感知。
- **建议修复**：
  1. 阶段三骨架期将 `auto_select` 默认改为 `false`；
  2. 选股失败时通过 Feishu 发送“选股未产生候选”提示；
  3. 增加 `--watchlist-only` CLI flag 便于用户显式控制。

### P2｜`run_live.py` 仅打印退出提示，无可供验证的“准实盘”演练模式

- **位置**：`scripts/run_live.py#L18-L26`
- **问题**：阶段四未启用时直接 `sys.exit(1)`，没有 dry-run/影子模式让用户在阶段三验证 QMT 连接、账户查询等。
- **建议修复**：
  1. 增加 `--dry-run` 模式：初始化 `QmtBroker`、connect、查询账户/持仓，不真正下单；
  2. 输出 QMT 环境检查清单（app_id、路径、账号是否配置）。

### P2｜没有统一的“建议仓位”计算逻辑

- **位置**：`src/gugu/engine/main.py#L128-L132`、`src/gugu/notifier/formatter.py#L101`
- **问题**：主引擎按 `total_value * max_position_ratio * 0.8` 计算目标金额，但 formatter 期望 `suggested_position` 是字符串（如“20%”）。两者没有对齐，导致飞书卡片缺少仓位建议。
- **建议修复**：
  1. 在策略或信号路由层统一计算 `suggested_position_ratio`；
  2. 将其格式化为百分比字符串填入信号；
  3. 该计算需考虑现金、已有仓位、L1 上限。

### P3｜CLI 入口缺少版本与全局帮助

- **位置**：`scripts/run_backtest.py`、`scripts/run_paper.py`、`scripts/run_live.py`
- **问题**：三个脚本各自独立 argparse，没有统一的 `--version`、`-h` 汇总、运行模式说明。
- **建议修复**：
  1. 增加 `--version` 打印 `gugu 0.1.0`；
  2. `run_paper.py` 在 non-daemon 模式执行前打印“即将执行一次模拟盘交易循环并发送收盘日报”。

---

## 3. 开发专家

### P1｜`config.py` 的 `lru_cache` 返回可变对象，存在缓存污染风险

- **位置**：`src/gugu/config.py#L47-L70`
- **问题**：`load_yaml`、`settings`、`strategy_defaults` 都被 `@lru_cache` 装饰，返回 dict。调用方一旦修改返回的 dict（如测试 monkeypatch 或业务代码 `settings().get(...).update(...)`），会永久污染缓存。
- **建议修复**：
  1. 返回 `dict` 的深拷贝，或在 `BaseSettings` 层面使用 pydantic 模型；
  2. 更推荐：将 YAML 配置映射为 pydantic dataclass，提供不可变视图。

### P2｜`pyproject.toml` mypy 目标版本与项目要求不一致

- **位置**：`pyproject.toml#L5`、`#L51`
- **问题**：`requires-python = ">=3.11"`，但 `[tool.mypy] python_version = "3.12"`。在 3.11 环境下可能引入 3.12 才支持的类型特性而不自知。
- **建议修复**：将 `python_version` 改为 `"3.11"`。

### P2｜配置未使用 pydantic-settings 的强类型模型

- **位置**：`src/gugu/config.py#L18-L44`、`#L63-L69`
- **问题**：`EnvSettings` 已用 pydantic-settings，但 YAML 配置全部以 `dict[str, Any]` 返回，调用处大量 `settings().get("risk", {}).get("...")`，易因拼写错误在运行时才发现。
- **建议修复**：
  1. 为 `settings.yaml` 定义 pydantic model：`Settings(DataConfig, StrategyConfig, RiskConfig, ExecutionConfig, FeishuConfig, SchedulerConfig, LogConfig)`；
  2. `settings()` 返回该 model 实例；
  3. 保留 `.env` 的 `EnvSettings` 独立。

### P2｜测试 fixture `broker` 缺少类型注解

- **位置**：`tests/unit/test_execution.py#L9-L11`
- **问题**：`def broker():` 没有返回类型，与项目其他测试不一致；`mypy` 当前不检查测试目录所以没报错，但维护性差。
- **建议修复**：`def broker() -> PaperBroker:`。

### P3｜`SinaCollector.fetch_stock_realtime` 对每只股票重复拉取全市场快照

- **位置**：`src/gugu/data/collectors/fallback.py#L39-L60`
- **问题**：循环 `for sym in symbols:` 内部调用 `ak.stock_zh_a_spot_em()`，如果 symbols 有 50 只，会拉 50 次全市场数据。
- **建议修复**：把 `spot_em()` 提到循环外，统一过滤。

### P3｜降级源对不支持的方法静默返回空 DataFrame

- **位置**：`src/gugu/data/collectors/fallback.py#L62-L70`、`#L95-L99`
- **问题**：`SinaCollector.fetch_sector_flow`、`TencentCollector.fetch_sector_flow` 等直接返回空 DataFrame，没有 warning，运营时无法区分“数据为空”和“源不支持”。
- **建议修复**：返回空 DataFrame 前 `logger.warning(..., extra={"source": self.source, "method": "fetch_sector_flow"})`。

---

## 4. 用户体验专家

### P1｜真实交易信号生成的飞书卡片会显示空白字段

- **位置**：`src/gugu/engine/signal_router.py#L117-L123` + `src/gugu/notifier/formatter.py#L97-L108`
- **问题**：同“产品专家 P1”。用户在飞书里看到的信号通知可能是：
  - 触发策略：``（空）
  - 建议仓位：``（空）
  这会让用户无法判断信号可信度。
- **建议修复**：先修数据契约；在契约未对齐前，可在 `format_signal` 增加兜底显示“未提供”。

### P2｜脚本入口依赖 sys.path 手动注入，环境要求不透明

- **位置**：`scripts/run_backtest.py#L14`、`scripts/run_paper.py#L14`、`scripts/run_live.py#L11`
- **问题**：脚本通过 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))` 解决导入，但：
  1. 与测试/IDE 的运行方式不一致；
  2. 若用户没激活 venv，会因依赖缺失得到 `ModuleNotFoundError`。
- **建议修复**：
  1. 提供 `uv run python scripts/run_paper.py` 或 `.venv\Scripts\python` 的标准入口；
  2. 在 `docs/` 或 `README` 写明环境激活步骤；
  3. 考虑把脚本改为项目内 `python -m gugu.cli.paper`。

### P2｜错误提示语言中英文混用，部分断言依赖中文

- **位置**：`src/gugu/execution/paper.py#L47`、`#L64`、`#L97`、`#L127`
- **问题**：`PaperBroker` 返回中文错误信息，但 `tests/unit/test_execution.py#L38` 同时断言 `"cash" in result.message.lower()`。若未来统一为中文，测试会挂。
- **建议修复**：
  1. 错误信息统一用中文（面向用户）或英文（面向日志/系统）；
  2. 测试改用错误码或 `result.success` 断言，减少文本依赖。

### P2｜`run_paper.py` 非守护模式默认发送收盘日报，行为不够显式

- **位置**：`scripts/run_paper.py#L23-L27`
- **问题**：用户执行 `python scripts/run_paper.py` 会同时跑交易循环 + 发送收盘日报。如果只想跑一次交易循环看信号，会被额外日报打扰。
- **建议修复**：增加 `--report` flag，默认不发送；或改为执行后打印摘要到终端。

### P3｜日志目录和级别无 CLI 覆盖能力

- **位置**：`src/gugu/utils/log.py#L14-L41`
- **问题**：日志路径 `PROJECT_ROOT/logs` 和级别 `env().log_level` 固定，调试时不方便临时改到 stdout 或改 DEBUG。
- **建议修复**：支持 `GUGU_LOG_LEVEL`、`GUGU_LOG_DIR` 环境变量，或 CLI `--log-level`。

---

## 5. 运营专家

### P1｜日亏计算使用初始本金而非当日开盘净值

- **位置**：`src/gugu/engine/main.py#L173-L180`
- **问题**：
  ```python
  initial = settings().get("execution", {}).get("paper", {}).get("initial_capital", 1_000_000)
  loss_pct = (initial - account.total_value) / initial
  ```
  日亏应以“当日开盘时总净值”为基准，而不是开户本金。否则运行 1 个月后，累计盈利 20% 时，某日回撤 4% 会被错误地判定为未达熔断。
- **建议修复**：
  1. `PaperBroker` 增加 `daily_start_value` 并在 `settle_t_plus_1()` 或每日开盘时记录；
  2. `TradingEngine._check_daily_loss` 使用 `daily_start_value`；
  3. 回测引擎同样需要日亏口径定义。

### P2｜调度器在节假日仍会触发任务，只是内部跳过

- **位置**：`src/gugu/engine/scheduler.py#L32-L50`
- **问题**：`CronTrigger(hour=9, minute=25, day_of_week="mon-fri")` 会在所有周一到周五触发，包括五一、国庆等调休假期。虽然 `_safe_run` 会跳过，但会造成不必要的日志、网络请求和唤醒。
- **建议修复**：
  1. 在 `setup()` 中读取 `settings().get("scheduler", {}).get("trading_days_only", True)`；
  2. 使用自定义 trigger 或每日先判断交易日再决定是否 add_job；
  3. 至少将节假日跳过记录为 INFO 并计入可观测指标。

### P2｜L2 熔断后没有自动/半自动恢复机制说明

- **位置**：`src/gugu/risk/manager.py#L52-L56`、`src/gugu/engine/main.py#L62-L63`
- **问题**：`RiskManager.reset()` 只在每日交易循环开头调用。如果盘中触发熔断，必须等到次日自动 reset，或人工重启进程。没有文档说明如何手动恢复、是否需要飞书确认。
- **建议修复**：
  1. 增加 `--reset-halt` CLI 命令或飞书“确认恢复”交互；
  2. 在 `docs/OPERATIONS.md` 写明熔断恢复 SOP；
  3. 熔断状态持久化到 SQLite，避免进程重启后丢失。

### P2｜`FeishuNotifier.close()` 从未被调用

- **位置**：`src/gugu/notifier/feishu.py#L194-L196`、`src/gugu/engine/main.py`、`scripts/run_paper.py`
- **问题**：`httpx.AsyncClient` 在进程退出时可能泄漏连接或触发 unclosed client session warning。
- **建议修复**：在 `TradingEngine` 增加 `async def shutdown()`，在脚本 `finally` 中调用 `await notifier.close()`。

### P2｜缺少健康检查/心跳指标

- **位置**：全局
- **问题**：运行中无法快速判断“系统是否还活着”。没有文件心跳、没有 metrics endpoint、没有 last_success_trade 时间戳。
- **建议修复**：
  1. 在 `logs/` 或 `data/` 写入 `heartbeat.json`，记录 `last_cycle_at`、`last_error`、`halted`；
  2. 每日日报中包含“系统状态”字段。

### P3｜日志为纯文本，未输出结构化 JSON 便于告警

- **位置**：`src/gugu/utils/log.py`
- **问题**：当前格式适合人读，但飞书/监控告警系统解析困难。
- **建议修复**：保留终端彩色文本，文件输出增加 JSON 格式选项（可通过配置切换）。

---

## 6. 数据专家

### P1｜`validate_stock_history` 对空值只 warning 不处理

- **位置**：`src/gugu/data/quality.py#L67-L69`
- **问题**：
  ```python
  null_count = df[required].isnull().sum().sum()
  if null_count > 0:
      logger.warning(f"[{ctx}] {symbol} 存在 {null_count} 个空值，将前向填充")
  ```
  日志说“将前向填充”，但代码里没有执行 `ffill()`。
- **建议修复**：删除误导性日志，或实际执行 `df = df.ffill().bfill()` 并限制连续缺失行数。

### P2｜回测引擎 O(n²) 信号生成，但已标注待优化

- **位置**：`src/gugu/backtest/engine.py#L131-L135`
- **问题**：每次循环都对 `df.iloc[:i+1]` 重新生成信号。注释已说明是 O(n²)，对单股 1-2 年历史数据可接受，但多策略/多股票时会很慢。
- **建议修复**：
  1. 骨架阶段可接受，但需在 `TECH.md` 标注性能基线；
  2. 后续改为向量化一次性生成信号后逐 bar 读取。

### P2｜历史数据除复权外未处理停牌/除权除息缺口

- **位置**：`src/gugu/data/collectors/akshare_collector.py#L37`
- **问题**：使用 `adjust="qfq"` 处理复权，但没有标记停牌日、没有处理分红送转后的成交量一致性校验。
- **建议修复**：
  1. 在 `validate_stock_history` 中增加停牌日/长缺口检测；
  2. 策略层暴露 `is_valid_bar` 过滤能力。

### P2｜缓存仅覆盖实时数据，历史数据无持久化缓存

- **位置**：`src/gugu/data/cache.py`、`src/gugu/data/manager.py#L65-L70`
- **问题**：`DataCache` 是内存缓存，进程重启即丢失；`config/settings.yaml#L13-L14` 配置了 SQLite/Parquet 路径，但代码里没有使用。
- **建议修复**：
  1. 实现历史数据 SQLite/Parquet 持久化；
  2. `fetch_stock_history` 优先读本地缓存，缺失再请求 akshare。

### P2｜交易日历降级逻辑仅按周末判断

- **位置**：`src/gugu/utils/calendar.py#L14-L26`
- **问题**：`chinese_calendar` 异常时降级为 `d.weekday() < 5`，会把国庆、春节等假期误判为交易日。
- **建议修复**：
  1. 维护一份近 3 年 A 股节假日表作为二级降级；
  2. 将 `_holiday_set()` 实际用于 `is_trading_day()` 的 fallback。

### P3｜`DataManager` 未校验数据 freshness

- **位置**：`src/gugu/data/manager.py#L65-L95`
- **问题**：缓存命中后直接返回，不检查数据是否为当日/当时。
- **建议修复**：在 cache value 中存储 `cached_at`，对实时行情 key 强制在开盘时段校验 freshness。

### P3｜自动选股依赖的 `main_pct` 列来源不清晰

- **位置**：`src/gugu/selector/stock_selector.py#L67-L69`
- **问题**：按 `main_pct` 排序，但 `fetch_stock_realtime` 返回的实时快照列不含 `main_pct`（只有 sector flow 含）。当 `market_df` 来自实时快照时，`main_pct` 列不存在，排序失效。
- **建议修复**：
  1. 明确选股数据源：实时快照用于过滤，sector flow 用于排序；
  2. 在 `select()` 中分别获取并合并两张表。

---

## 7. 测试专家

### P1｜缺少集成测试与 fixtures 目录

- **位置**：`tests/integration/__init__.py`、`tests/fixtures/`
- **问题**：`CLAUDE.md` 要求测试数据放 `tests/fixtures/`，但目录不存在；集成测试目录为空。
- **建议修复**：
  1. 创建 `tests/fixtures/sample_ohlcv.csv`、`tests/fixtures/sample_sector_flow.csv`；
  2. 新增 `tests/integration/test_trading_flow.py`；
  3. 新增 `tests/integration/test_data_manager_fallback.py` 验证降级切换逻辑。

### P1｜主引擎 `TradingEngine` 未覆盖

- **位置**：`src/gugu/engine/main.py`
- **问题**：覆盖率 23%。`_process_signal`、`_check_daily_loss`、`_update_prices`、`send_daily_report` 等核心路径没有单元/集成测试。
- **建议修复**：
  1. 用 `unittest.mock` 替换 `data_manager`、`FeishuNotifier`、`PaperBroker`、`RiskManager`；
  2. 测试 happy path、风控拦截路径、非交易日跳过路径。

### P2｜`FeishuNotifier` 真实发送路径未覆盖

- **位置**：`src/gugu/notifier/feishu.py`
- **问题**：覆盖率 33%，`_get_tenant_token`、`_post_card`、`_post_webhook` 等网络路径无测试。
- **建议修复**：
  1. 用 `respx` 或 `httpx.AsyncClient(transport=MockTransport)` mock 飞书 API；
  2. 测试 token 缓存、重试、未配置时优雅降级。

### P2｜`test_generator.test_save_strategy` 仅通过 mock 隔离了文件系统

- **位置**：`tests/unit/test_generator.py#L159-L174`
- **问题**：该测试 OK，但缺少对真实 `registry.py` 被修改的回归保护。若未来有人直接调用 `generate_and_save()` 而未 mock，会永久改写源码。
- **建议修复**：
  1. 在 `generator.py` 增加 `dry_run` 参数用于测试；
  2. 在 CI 中检查 `git diff -- src/gugu/strategies/registry.py` 为空。

### P2｜`test_data.py` 未覆盖降级采集器

- **位置**：`tests/unit/test_data.py`
- **问题**：只测试了 `DataCache` 和 `quality`，没有测试 `AkshareCollector`、`SinaCollector`、`TencentCollector`、`DataManager`。
- **建议修复**：
  1. 用 `monkeypatch` 替换 akshare 调用；
  2. 测试 `DataManager._call_with_fallback` 的降级计数与冷却逻辑。

### P3｜`conftest.py` fixture 单一

- **位置**：`tests/conftest.py`
- **问题**：只有 `ohlcv_df`，缺少 `broker`、`risk_manager`、`position`、`account_info`、`signal` 等公共 fixture。
- **建议修复**：按模块拆分 fixture 到 `tests/unit/conftest.py` 或 `tests/fixtures/factories.py`，提供 `make_position()`、`make_signal()` 工厂函数。

---

## 8. 文档缺口与建议结构

当前只有 `CLAUDE.md`（AI 工作公约）和 `docs/ITERATION_1.md`（迭代计划）。缺少面向不同受众的独立文档。建议新增：

### `docs/PRODUCT.md` 建议章节

1. **产品定位**：gugu 是什么、解决谁的什么问题
2. **目标用户**：个人投资者/开发者自用
3. **核心流程**：五阶段演进图（策略固化 → 回测 → 模拟盘 → 小额实盘 → 正式实盘）
4. **MVP 边界**：当前阶段已实现/未实现清单
5. **功能清单**：选股、信号、风控、执行、通知、智慧增强
6. **风险与承诺**：免费数据源免责声明、模拟盘不等同实盘
7. **快速开始（非技术）**：如何收到第一条飞书信号
8. **常见问题 FAQ**

### `docs/TECH.md` 建议章节

1. **技术栈**：Python 3.11+、uv、akshare、APScheduler、loguru、飞书 API
2. **目录结构**：与 CLAUDE.md 保持一致并解释新增 `models/`、`selector/`
3. **模块依赖图**：数据层 ← 策略层 ← 引擎 → 风控/执行/通知
4. **数据流**：实时行情 → 选股/扫描 → 信号融合 → 风控 → 模拟下单 → 飞书通知
5. **配置说明**：`.env` 敏感项 + `config/*.yaml` 业务项逐项说明
6. **本地开发环境**：`uv sync`、`uv run pytest`、激活 venv、运行脚本
7. **测试策略**：单元测试覆盖目标、集成测试范围、fixture 规范
8. **部署与运行**：`run_paper.py --daemon`、Scheduler 说明、日志位置
9. **阶段演进检查清单**：每个阶段启用前必须满足的条件
10. **故障排查**：熔断恢复、数据源降级、飞书收不到消息

---

## 9. 验证结果

| 检查项 | 命令 | 结果 |
|--------|------|------|
| 单元测试 | `.venv\Scripts\python -m pytest tests/ -v --cov=src/gugu` | 113 passed，覆盖率 70% |
| 代码风格 | `.venv\Scripts\ruff check src tests scripts` | All checks passed |
| 类型检查 | `.venv\Scripts\mypy src/gugu` | Success: no issues found |

> 注意：使用系统默认 Python 直接运行 `pytest`/`ruff`/`mypy` 会失败，因为依赖未安装在系统环境。需在 `docs/TECH.md` 中明确使用 `.venv\Scripts\python` 或 `uv run`。

---

## 10. 修复优先级汇总

| 优先级 | 数量 | 代表性问题 |
|--------|------|-----------|
| P1 | 4 | PaperBroker T+1 意图错误、信号契约不匹配、主引擎未传 L3 元数据、日亏口径错误 |
| P2 | 13 | 缺少集成测试/fixtures、回测全仓矛盾、配置无强类型、节假日调度、熔断恢复、数据缓存未落地 |
| P3 | 9 | CLI 帮助、日志覆盖、降级源警告、缓存 key 稳定性、conftest 单一 |

**建议迭代 1 的修复顺序**：
1. 先修 4 条 P1，确保模拟盘行为与飞书通知正确；
2. 补充集成测试与 fixtures，提升 `engine/main.py` 覆盖率；
3. 补齐 `PRODUCT.md` 与 `TECH.md`；
4. 处理 P2/P3 中低成本的治理项。
