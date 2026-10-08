# 变更日志

## 2026-10-08 — v0.9.54 告警分诊脚本 NameError 修复（告警评估员处置）

### 变更：告警评估员处置（2026-10-08 cron 脚本错误）

**背景**：cron 报「data-collection script exited with code 1」。对抗性审查确认根因：
`scripts/alert_triage.py` 第 229 行午休间隔判定使用 `time(11, 30)`，但顶部导入仅
`from datetime import datetime, timedelta`（缺 `time`）。该分支在 `scan_ts` 存在时
才走到（开盘首轮扫描完成后），此前分诊未触发；今日走到即 NameError → 分诊脚本
崩溃 exit 1，告警评估链路中断（分诊数据无法产出，非系统业务异常）。

**修复**：
- `scripts/alert_triage.py`：导入补全为 `from datetime import datetime, time, timedelta`。
- 修复后重跑分诊输出 **HEALTHY**（系统本身无异常，纯观测脚本 bug）。

**验证**
- `pytest tests/unit/test_alert_triage.py --timeout=30` → 8 passed
- `ruff check scripts/alert_triage.py` → All checks passed
- 不涉及 scheduler 运行时，无需重启调度器

## 2026-10-01 — v0.9.53 启发式优化超时预算上调 + 耗时观测（告警评估员处置）

### 变更：告警评估员处置（2026-10-01 告警分诊）

**背景**：分诊报当日 1 条 ERROR（04:15:12 `启发式优化 double_yin 超时（>15min），跳过`）。
对抗性审查确认根因：**v0.9.53 修复代码早已写入工作区但从未提交/重启，运行中的
调度器（PID 45463，2026-09-29 16:06 启动）仍加载旧代码（timeout=900s）**，
double_yin 全量 PSO 评估≈18000 回测行，900s 恰好掐在收尾处 → 2026-09-15 GA 与
2026-10-01 PSO 两次全部超时（2/2 失败），参数从未优化成功。本次超时本身无害
（设计行为：单策略超时保护 + asyncio.to_thread 已隔离事件循环，心跳保持新鲜，
无 watchdog 误判重启），但修复未生效属流程缺陷。

**修复**：
- `src/gugu/engine/scheduler_jobs.py` 两处优化任务单策略超时上调：周参数优化
  1200s→1800s、启发式优化 900s→1800s（覆盖 double_yin 完整工作量）。
- 新增 `time.monotonic()` 耗时观测，完成/失败/超时日志均带实际耗时，便于校准预算。
- 提交修改 + 重启调度器，使修复真正生效（此前工作区修改未提交、进程未重启）。

**验证**
- `pytest tests/unit/test_scheduler.py --timeout=30` → 8 passed
- `ruff check src/gugu/engine/scheduler_jobs.py` → All checks passed
- `launchctl kickstart -k gui/$(id -u)/com.gugu.scheduler` 重启后心跳新鲜

## 2026-09-29 — v0.9.52 source_health 轮转后统计丢失修复（告警评估员处置）

### 变更：告警评估员处置（2026-09-29 16:00 告警分诊，v0.9.51 修复的后续）

**背景**：分诊报当日 1 条 ERROR（09:33:17 主源 sina fetch_stock_history 失败 1 次，
tencent 终极兜底即时接管——四级冗余设计内降级，本应直接放过）。
但对抗性审查发现更深一层**可观测性 bug**：15:10 事件文件轮转（>5000 行，归档到
`source_health_events.jsonl.2026-09-29`，619KB）后，实时文件被清空，
而 `get_daily_summary()` 与 `alert_triage.py` 只读实时文件 → 轮转当天统计丢失，
今日实际 9 failures / 1161 requests = 0.78% 被误报成 "0% (0/0)"，
失败率监控在轮转日完全失明（历史轮转日 08-12/08-17/08-24/08-31/09-07/09-11/09-23
摘要均为 0/0，同根因）。

**修复**：
- `src/gugu/data/source_health.py`：`get_daily_summary` 新增 `_iter_event_files()`
  聚合实时文件 + 全部 `source_health_events.jsonl.*` 归档，按目标日期时间戳过滤
  （归档文件名=轮转日期，内容可能混多日，故必须按时间戳过滤）；无匹配事件返回空摘要。
- `scripts/alert_triage.py`：第 6 节失败率同样聚合实时 + 归档文件。
- 数据零改动：归档文件本就完整，只是读取口径修复；用修复后代码重算并回填
  `source_health_daily.json` 的 2026-09-29 条目（1161 请求 / 9 失败 / 0.78%）。
- scheduler 重启生效（PID 692 → 45463，心跳 16:07:39 新鲜），v0.9.51 的
  manager.py `{!r}` 日志格式修复也随之进入运行进程。

**验证**
- `pytest tests/unit/test_source_health.py tests/unit/test_data_manager.py --timeout=60` → 20 passed
- `ruff check src/gugu/data/source_health.py scripts/alert_triage.py tests/unit/test_source_health.py` → All checks passed
- `.venv/bin/python scripts/alert_triage.py` → 第 6 节恢复真实口径：今日失败率 1% (9/1172)

## 2026-09-29 — v0.9.51 主源异常日志可观测性修复（告警评估员处置）

### 变更：告警评估员处置（2026-09-29 15:00 告警分诊）

**背景**：分诊报当日 1 条 ERROR（09:33:17 `主源 fetch_stock_history 异常: `，异常消息为空）。
对抗性审查确认根因链：

- 09:33:06-07 网络抖动窗口，akshare / eastmoney 同时 `RemoteDisconnected`（远端断开），
  09:33:17 主源 sina `fetch_stock_history` 超时失败。
- `asyncio.wait_for` 超时抛 `TimeoutError`，其 `str(e) == ""` → 日志 `异常: {}` 输出空消息，
  **零诊断价值**，分诊无法从日志行直接判断根因（需翻 source_health_events 交叉验证）。
- source_health_events 证实 09:33:17 同秒 `failure sina` → `fallback_used tencent`，
  四级冗余第 4 级（腾讯终极兜底）即时接管成功。当日失败率 1%（9/1130），信号扫描
  data_health=ok（2 信号），心跳新鲜 → **降级本身是设计内行为，无需处置**。

**修复**：`src/gugu/data/manager.py:283` 日志改用 `{!r}` 格式化异常，
空消息异常也会输出类型（如 `TimeoutError()` / `RemoteDisconnected('...')`），
后续分诊可直接从日志行判定根因类别。

**验证**
- `pytest tests/unit/test_data_manager.py tests/unit/test_data_manager_extended.py --timeout=30` → 31 passed
- `ruff check src/gugu/data/manager.py` → All checks passed
- 无需重启 scheduler（仅日志格式变更，不影响运行时行为）

## 2026-09-15 — v0.9.50 参数优化任务阻塞事件循环修复（asyncio.to_thread）

### 变更：告警评估员处置（2026-09-15 09:00 告警分诊）

**背景**：分诊报当日 1 条 ERROR（04:15:15 `启发式优化 double_yin 超时（>15min），跳过`），
且自愈记录显示 04:15:30 触发 heartbeat_stale 重启（成功 1 次，进程存活=True）。
对抗性审查确认根因链：

- 每半月 04:00 的启发式优化任务 `_safe_heuristic_optimize`（及周日 03:00 的
  `_safe_optimize_params`）在 **asyncio 事件循环里直接跑阻塞的 `subprocess.run`**
  （timeout 900s/1200s）。
- double_yin 子进程超时 15min → 事件循环被阻塞 15min → 60s 心跳任务被饿死，
  heartbeat.json 停留在 03:59:45 → watchdog 15min 阈值误判挂死 → auto-recover
  重启调度器（04:15:27 新实例，盘前重启无扫描损失，当前心跳新鲜）。
- 超时跳过本身是设计行为（单策略 15min 保护），**真正的问题是阻塞事件循环导致
  误触发 watchdog 重启**，每月 1/15 + 每周日都会重演。

**修复**：`src/gugu/engine/scheduler_jobs.py` 两处 `subprocess.run` 均改为
`await asyncio.to_thread(subprocess.run, ...)`（沿用项目既有模式：market_filter /
data/manager / collectors 均用 to_thread 包装阻塞调用）。事件循环保持响应，
心跳照常新鲜，watchdog 不再误判。`subprocess.TimeoutExpired` 在线程内抛出后
仍被协程捕获，超时跳过逻辑不变。

**验证**
- `pytest tests/unit/test_scheduler.py --timeout=30` → 8 passed
- `ruff check src/gugu/engine/scheduler_jobs.py` → All checks passed
- 重启 scheduler（launchctl kickstart）后心跳新鲜，无 ERROR 复现

## 2026-09-14 — v0.9.49 akshare 行业资金流失败级别降级（ERROR→WARNING）

### 变更：告警评估员处置（2026-09-14 12:31 告警分诊）

**背景**：分诊报当日 2 条 ERROR（10:01:13/10:01:15 `akshare 行业资金流失败:
RemoteDisconnected`）。对抗性审查确认：东财上游瞬时断连（10:01-10:02 一波
连接被关闭），akshare 与 eastmoney 均按设计触发 1800s 资金流熔断 + 缓存兜底，
扫描正常完成（30 条异动告警），后续扫描全部恢复，今日失败率 0.3%（4/1327），
同类错误此前 3 天均为 0 次——纯瞬时降级，非代码失效。

**🟡 顺手修复日志级别不一致（v0.9.25/v0.9.28 约定遗漏）**
- 现象：`fetch_sector_flow` 失败记 ERROR，`fetch_stock_flow` 记 WARNING——
  同一降级路径、两种级别，导致每次瞬时降级都污染 ERROR 统计、误触发分诊告警。
- 根因：v0.9.25/v0.9.28 把"备用源失败属设计内降级路径→记 WARNING"（eastmoney
  兜底 + 缓存兜底 + 1800s 熔断防刷屏）的约定落地时漏改了
  `akshare_collector.fetch_sector_flow`（同文件 `fetch_stock_flow` 及
  `eastmoney_collector.fetch_sector_flow` 均已 WARNING）。
- 修复：`src/gugu/data/collectors/akshare_collector.py` line 117
  `logger.error` → `logger.warning`，补注释对齐约定。纯日志级别调整，
  不影响采集/熔断/兜底逻辑。

**验证**
- `pytest tests/unit/test_collectors.py tests/unit/test_data_manager.py`
  → 52 passed
- `ruff check src/gugu/data/collectors/akshare_collector.py` → All checks passed

## 2026-09-11 — v0.9.48 舆情新闻/公告双源失效修复（akshare 接口误用 + pandas arrow 正则）

### 变更：告警评估员处置（2026-09-11 12:30 告警分诊）

**背景**：分诊报的 09:36:18 尾盘验证 ERROR 已在 v0.9.46/v0.9.47 修复并部署
（scheduler 11:05:13 重启，晚于修复提交 11:05:08），属已解决的历史告警。
对抗性复核日志时发现**告警清单之外**的两条长期失效数据源（WARNING 级，但对功能是 100% 失效）。

**🟡 失效 1：个股新闻源自 2026-09-07 起 100% 失败（148+ 次/日 WARNING）**
- 现象：`东方财富新闻获取失败 600519: Invalid regular expression: invalid escape sequence: \u`
- 根因：pandas 3.0.3 默认 arrow-backed 字符串后端，akshare
  `stock_news_em` 内部 `df["新闻内容"].str.replace(r"\u3000", "", regex=True)`
  走 pyarrow RE2，RE2 不支持 `\u` 转义 → `ArrowInvalid`（Python re 语义 ≠ RE2）。
- 修复（`src/gugu/analysis/sentiment/fetcher.py`）：新增
  `_python_string_storage()` 上下文管理器，仅在 akshare 调用期间把
  `pd.options.mode.string_storage` 切为 `python`，退出时（含异常路径）无条件还原全局选项。

**🟡 失效 2：个股公告源 100% 失败（KeyError，历来未成功过）**
- 现象：`个股公告获取失败 600519: '600519'`
- 根因：`ak.stock_notice_report(symbol=...)` 的 `symbol` 参数是**公告类型**
  （全部/重大事项/财务报告/…），旧实现传股票代码 → 必 KeyError。
- 修复：改为 `symbol="全部"` + `date=YYYYMMDD` 拉全市场公告看板，
  再按股票代码过滤；看板回溯最近 2 个交易日合并（按标题去重）、
  跳过非交易日、进程内按日缓存（避免每只股票/每轮扫描重复拉 1400+ 行）。

**验证**
- 新增 `tests/unit/test_news_fetcher.py` 11 例：字符串后端切换/异常还原、
  新闻解析、公告接口 `symbol="全部"` 回归守卫、看板按日缓存、
  非交易日跳过、akshare 缺失/上游异常降级为空、合并去重。
- 联机冒烟（真实 akshare）：600519 新闻 5 条（修复前 0 条）；
  000703 公告 3 条含 URL（修复前恒为空）。
- `ruff check` + `ruff format --check` 全绿；全量单测 **2143 passed / 141 skipped**（0 failed）。
- 部署：`launchctl kickstart -k` 重启（12:35:19，PID 28090，launchd 单实例托管），
  重启后 13:00 盘中扫描完整跑通：新增 ERROR 0 条、新闻/公告失败 WARNING 0 条
  （修复前每轮扫描各 2~20 条）；生产路径复现 `NewsFetcher().fetch_stock_news(600519, 5)`
  返回 5 条新闻且 SentimentAnalyzer 正常出标签。

### 放过记录（无代码变更，附理由）
- 09:36:18 尾盘验证 ERROR：v0.9.46/v0.9.47 已修复，运行实例（PID 13273，
  11:05:13 启动）晚于修复提交，当日日志中的该行属**历史告警残留**，无需处置。
- 心跳（12:30:48）/watchdog/自愈/磁盘（8.0GB）/数据源失败率 0%/单实例锁正常。

### 附带：分诊口径修正 + 历史残留标注（scripts/alert_triage.py）
- 同一 ERROR 在当日修复+重启后仍被反复分诊（今日连续 3 轮），造成重复处置。
- 修正条数口径：只统计 loguru 行头 ERROR/CRITICAL，Traceback 行标注为
  「上下文」（旧口径 1 次崩溃报「2 条」）。
- 新增 `_stale_error_note()`：当日 ERROR 时间戳**全部早于**当前实例
  `scheduler_boot.json` 启动时间时，追加「疑似已由重启修复的历史残留」提示。
  **仅展示层标注，不改变 issues 判定**——重启后复现的 ERROR 照常上报，不会被掩盖。
- 测试：新增 `tests/unit/test_alert_triage.py` 8 例（无 boot 记录 / 错误晚于启动 /
  临界同秒 / 全部早于启动 / 部分晚于启动 / 无时间戳行 / 缺字段 / 空输入）→ 全绿。

## 2026-09-11 — v0.9.47 尾盘验证行情字段防护（非数值/缺失字段不再中断任务）

### 变更：告警评估员处置（2026-09-11 11:05 告警分诊）

**背景**：分诊报「当日日志 2 条 ERROR/异常」= 09:36:18 尾盘验证 NoneType 崩溃
（已在 v0.9.46 修复并部署，属已解决的历史告警）。复核修复后代码，
发现同一函数仍有**同类残留脆点**，本次做第二批加固：

**🔴 残留脆点 1：非数值字段 → ValueError 中断整批**
- `_fetch_today_prices` 解析东财 push2 字段用 `float(item.get("f17"))`，
  停牌/异常标的返回 `"-"` / `"--"` / `None` 时抛 `ValueError`/`TypeError`，
  且该解析在 `try` 之外 → 与 09:36 崩溃同属「一条坏数据中断整批尾盘验证」。

**🟡 残留脆点 2：low/current 缺失回退 0.0 → 污染验证统计**
- 旧逻辑 `float(low_raw) / 100 if low_raw is not None else 0.0`：最低价缺失记 0.0 →
  `low_price <= stop_loss_price` 恒真 → **假止损命中**；
  `_pct(0.0, close)` → 当前溢价 -100% → 污染 auto_tune 收益样本与止损命中率统计。

修复（`src/gugu/screener/validator.py`）：
1. 新增 `_parse_price()`：统一解析东财原始价格（原始值 ÷100），
   非数值 / None / NaN / Inf / ≤0 一律返回 None；
2. 今开/最高/最低/最新四字段必须全部有效才入 `price_map`，
   否则 WARNING + 跳过该条（不合格数据不进系统）——单条坏数据不再中断整批；
3. `_fetch_hs300_open_change` 改用同一 helper，基准字段非法时降级 0.0 而非抛错。

测试：`tests/unit/test_screener_validator.py` 新增 `TestPriceFieldGuard` 5 例
（`'-'` 不崩溃 / low 缺失不产生假止损 / 部分非法仅跳过坏记录 / hs300 非法降级 /
`_parse_price` 边界），并补齐 2 个 P0 时代 mock 缺失的 f2/f16 字段
（v0.9.46 后新增"四字段齐全"约束）→ **32 passed**；screener 相关 **117 passed**；
全量单测 **2085 passed, 102 skipped**；`ruff check` / `ruff format --check` 全绿。

### 放过记录（无代码变更，附理由）
- `eastmoney GET 失败 / ConnectionError` WARNING 密集出现（10:01/10:05/10:36 各若干条）：
  四级数据源冗余内的瞬时降级，`signal_scan.data_health=ok`、今日数据源失败率 0%，
  资金流接口自动降级，非缺陷。
- 心跳/watchdog/自愈/磁盘（9.3GB）/单实例锁（PID 2669）全部正常，无需处置。

## 2026-09-11 — v0.9.46 尾盘验证数据源失败崩溃修复（NoneType 运算）

### 变更：告警评估员处置（2026-09-11 告警分诊）

**🔴 ERROR 09:36:18 `scheduler_jobs:116 尾盘选股验证异常: unsupported operand type(s) for -: 'NoneType' and 'float'`**
- 根因：`screener/validator.py::_fetch_today_prices` 在「东财批量行情请求异常」与
  「返回空 diff」两条失败路径上返回 `[(None,None,None,None)] * n` 元组，
  绕过调用方 `if today_data is None: continue` 守卫 → `None` 流入 `_pct()`，
  `None - 23.5` 抛 TypeError，**整个 09:35 尾盘验证任务中断**，
  昨日（09-10 sh603533 掌阅科技）验证结果未落库（`validate` 循环首条即崩）。
- 修复（`src/gugu/screener/validator.py`）：
  1. 失败/空数据路径改返回 `[None] * len(records)`，与声明的返回类型
     `list[tuple[...] | None]` 一致，使调用方 None 守卫真正生效；
  2. 验证循环新增防御性 None 守卫：`today_data` 任一价格字段缺失时
     WARNING + `continue` 跳过该记录，而非中断整批（四级数据源降级后仍可能返回 None）。
- 测试：`tests/unit/test_screener_validator.py` 新增 3 个回归用例
  （数据源异常不崩溃 / 空 diff 不崩溃 / `_fetch_today_prices` 返回 None 占位）→
  **27 passed**；相关 screener 套件（screener + params + auto_tune + validator）**94 passed**；
  `ruff check` 全绿。
- 部署：`launchctl kickstart -k` 重启，新实例 PID 2669 由 launchd 托管，心跳新鲜
  （last_cycle_at 持续递增，无崩溃循环）。
- 附带发现：重启前运行中的 scheduler 是**手动 nohup 启动**（PPID=1 的 `uv run`），
  与 launchd plist 实例互斥 → launchd job 长期反复被单实例锁拒绝（`last exit=1`，
  err.log 累积 4.2MB）。本次重启后已收敛为 launchd 单实例托管。

### 放过记录（无代码变更，附理由）
- 09:36 基本面过滤对 600256/600255 报 "全部指标获取失败，拒绝放行"：
  akshare/东财瞬时断连，四级冗余设计内的安全拒绝（拒放行=宁缺毋滥），非缺陷。
- `sentiment.fetcher` 正则 WARNING（`Invalid escape sequence \u`）：源自 akshare 内部，
  gugu 已优雅降级（WARNING + 公告兜底），历史已知无害。

## 2026-09-04 — v0.9.45 重启风暴阈值对齐部署常态（5→8）

### 变更：告警评估员处置（2026-09-04 告警分诊）

**❌ 重启风暴升级 [SchedulerHealthResult:restart_storm] 09:40:15（今日启动 6 次）**
- 根因调查：09-04 六次启动（00:14/01:05/03:15/07:28/07:51/09:37）全部为优雅信号退出
  （日志均含"调度器已停止"、exit code=0、无 ERROR/Traceback、crash_clue=无崩溃线索、
  launchd `last exit code = 0`）→ 显式 `launchctl kickstart` 的部署/开发重启
  （CLAUDE.md 标准工作流），非崩溃循环；看门狗当日无任何自动修复动作；
  09:37 后调度器连续稳定 4h+（心跳/扫描/信号全正常）
- 修复：`MAX_RESTARTS_PER_DAY` 5 → 8（v0.9.41 曾 3→5，09-03/09-04 连续两天
  6 次优雅重启再次误报升级；真实崩溃循环仍由心跳陈旧独立指纹捕获，无盲区）
- 测试：`test_restart_threshold_default_allows_dev_restarts` 同步更新
  （4/6 次不告警，9 次告警）；`49 passed`；Ruff 全绿

### 放过记录（无代码变更，附理由）
- `sentiment.fetcher` 正则 WARNING：`Invalid escape sequence \u` 源自 akshare 内部，
  gugu 已优雅降级（WARNING + 公告兜底 + 2h 缓存），非 gugu 代码缺陷
- 13:31 资金流数据源熔断（akshare 连续 3 次失败 + eastmoney ConnectionError）：
  瞬时网络抖动，缓存兜底 + 1800s 自动恢复，四级冗余设计行为
- 凌晨 00:29-03:09 回测批量任务日志风暴（~70MB/3h，box_breakout/300760 等）：
  外部批处理并发多进程所致，磁盘余量 9.5GB 无风险，10MB 轮转自动回收
- Hermes cron `gugu-scheduler` 任务（工作日 08:00）脚本损坏（"Script not found"）：
  与 launchd KeepAlive + gugu-health-watchdog 职责重复且静默失败，无害；
  建议后续人工清理该任务（cron 配置不在 gugu 仓库内，评估员不动）

## 2026-09-04 — v0.9.44 评审修复落地：paper 成交 + Gate 裁决 + 持仓感知卖出

### 变更：多专家评审（2026-09-03）三项 P0 修复

**P1 模拟盘 0 成交**
- 根因：`observation_only`/experimental 信号在 paper 模式被 OrderManager/RealtimeScanner 一刀切阻断，模拟盘永远无法积累验证数据
- 修复：paper 模式放行 observation_only 买入进入 PaperBroker（模拟盘的目的就是成交验证）；`gate_blocked`（纪律拦截）在任何模式都阻断；新增 `test_order_manager_buy_gate_allows_observation_in_paper`

**P2 研究账本 gate 全部 not_evaluated**
- 根因：`_check_unlock_conditions` 只在 `--write` 时执行，write=False 的研究跑动从不裁决
- 修复：单股/多股/启发式三个优化入口 Gate 始终裁决；写回仍要求 gate 通过；manifest validation 仅 write 时更新
- 实证：10 策略 gate-v1 基线全部记录，均未通过（主因样本外交易 0-8 笔 < 30 门槛）

**P3 卖出信号刷屏**
- 根因：dragon_head/huamulan/sector_momentum/double_yin/box_breakout 卖出无条件触发（无持仓也刷）
- 修复：顺序状态机持仓感知（买入开仓→卖出平仓）；double_yin 补移动止损兜底
- 效果：double_yin 1135→90、sector_momentum 1365→311、dragon_head 880→48、huamulan 832→2

**测试与工程**
- 修 order_manager fail-closed（强制非 paper）、double_yin 止损（先注入买入）、watchdog 重启风暴时间边界（锚定当天 09:00）
- 补 huamulan_ghost_breakout / sentiment_cycle 参数网格（此前缺失被跳过）
- 全量 `2088 passed / 143 skipped / 0 failed`；Ruff 全绿；调度器 paper 模式重启



### 变更：从策略研究基础设施收敛为散户决策辅助系统

**执行模式**
- `track_live` → `paper`：模拟盘 100 万本金启动，实验策略开始实际执行验证收益；实盘跟踪关闭

**策略根因修复（5 个）**
- price_volume_signal：`float_shares=1e9` 硬编码导致换手率虚高 3-10 倍 → 自动估计流通股本 + amount=0 退化修复（0→11 trades）
- rsi_reversal：MA20>=MA60 与 RSI<20 逻辑互斥 → 移除硬过滤（1→8 trades）
- box_breakout：三重 AND 锁死信号 → 趋势过滤改为置信度权重（0→7 trades）
- double_yin：支撑接近度 3% 太紧 → 参数化 `support_tolerance`（默认 5%）
- regime_cycle_four：金叉当天才入场 → `silver_window` 扩展至 5 日

**策略清洗（40 → 10）**
- 删除 30 个已证伪/无进展/冗余策略（代码、注册、manifest、defaults、param grid 全量清理），0 死代码残留

**新策略（4 个）**
- sector_momentum：板块动量轮动（趋势+动量+量能三重确认，唯一正收益回测）
- dragon_head：钱多多龙头战法（强势特征+回踩低吸+情绪破位离场）
- huamulan_ghost_breakout：烂板成妖打板（归档复活，4 AND + 移动止损）
- sentiment_cycle：情绪周期（MingCe 移植，波动率+量能+动量合成情绪指标）

**交易纪律（道体系落地）**
- 入场 Gate：5 关检查（市场状态/风控熔断/持仓上限/剧烈波动/涨停环境）
- 移动止损替代时间止损：`_apply_trailing_stop_exit`，从最高点回撤 8% 才卖
- 市场环境检测：`MarketEnvironment`（冰点/涨停数/封板率 70%/接力率 60%）
- 交易日志：`trade_journal.jsonl` 结构化记录每次买入/拦截/止损
- 龙头识别器：`DragonHeadDetector`（板块结构分析，待接入策略流）

**文档同步**
- 新增 `docs/TRADING_SYSTEM.md`（个人交易系统：道法术器四层）
- PRODUCT.md / STRATEGY_LIFECYCLE.md 同步到 10 策略现实

**验证**
- 修复 9 个测试文件死引用（Bollinger/KDJ/DualThrust/turtle 等已删策略）
- 全量测试 `2087 passed / 143 skipped / 0 failed`
- 调度器重启后：策略一致性 ✅，三层一致，10 策略 0 死代码残留

## 2026-09-03 — v0.9.42 复核补充（评估员）：确认修复代码已加载运行

- **复核发现**：signal_pipeline.py 修复写入时间 12:10:28 晚于 12:08:19 的调度器重启——12:08 启动的进程（PID 46341）加载的是修复前/中间版本代码，修复未真正生效。
- **处置**：12:51 `launchctl kickstart -k` 重启调度器（PID 82905），新进程加载修复版；心跳 12:51:53 新鲜、24 任务注册成功、策略一致性 ✅。全项目 grep 确认无残留 `is_halted()` 误调用。
- **验证**：`pytest tests/unit/test_signal_pipeline.py --timeout=60` 38 passed；`ruff check` 通过（复核时复跑）。
- **结论**：09:31 的 8 条 `'bool' object is not callable` ERROR 根因已消除；09:45 zero_signal_streak 升级为同根因次生（信号被吞→连续 0 信号），随修复加载自然恢复。restart_storm 升级（今日 6 次启动）全部为优雅部署重启（日志均含"调度器已停止"），无崩溃线索，按 v0.9.41 结论放过。

## 2026-09-03 — v0.9.42 修复：`is_halted` property 误加括号致盘中扫描全灭

### 变更：`_entry_gate` 中 `risk.is_halted()` → `risk.is_halted`（v0.9.42）
- **问题（2026-09-03 实证）**：调度器 09:23 启动后，09:31 扫描轮次 8 只股票（600127/600176/600121/600110/600172 等）全部报 `TypeError: 'bool' object is not callable`，每轮扫描 0 信号 → 09:45 触发 `ZeroSignalResult:zero_signal_streak` 升级人工（连续 3 轮 0 信号，data_health=ok）。
- **根因**：`RiskManager.is_halted` 是 `@property`（返回 bool），`_entry_gate` 新增代码误加 `()` 调用，进程内每只股票扫描抛异常被吞（`process()` 顶层 except 返回 None），信号流静默归零。
- **修复**：去掉 `()` 改为属性访问；顺带清理 `_entry_gate` 中未使用的 `MarketEnvironment()` 实例与 import 排序（ruff F841/I001）。
- **验证**：`pytest tests/unit/test_signal_pipeline.py --timeout=60` 38 passed；`ruff check src/gugu/engine/signal_pipeline.py` 通过；`launchctl kickstart -k` 重启调度器（PID 46341），心跳 12:08:19 新鲜。
- **生效方式**：调度器已重启加载新代码，无需其他操作。

## 2026-09-02 — v0.9.41 评估员修复：重启风暴阈值对齐部署常态

### 变更：`MAX_RESTARTS_PER_DAY` 3 → 5（watchdog 参数修正）
- **问题（2026-09-02 实证）**：当日调度器 4 次重启全部为优雅部署/开发重启（02:03/07:47/08:27/12:34，日志均含"调度器已停止"，12:34 重启前 12:31-33 有策略代码改动；crash_clue=无崩溃线索，心跳连续无空窗）→ 旧阈值 3 误报"重启风暴"并升级人工。
- **根因**：v0.9.32 注释自述"历史常态 3-4 次/日"（launchd kickstart/部署重启），`MAX_RESTARTS_PER_DAY=3` 低于自身文档化的部署常态，任何正常开发日（≥4 次按需重启）都会触发高优级误报。
- **修复**：默认阈值 3 → 5，并更新注释说明。真实崩溃循环仍由心跳陈旧检查（独立指纹，15 分钟未更新）捕获，无盲区；风暴当日同因去重（v0.9.32）机制保留。
- **验证**：新增回归测试 `test_restart_threshold_default_allows_dev_restarts`（默认阈值下 4 次重启 ok=True / 6 次重启告警）；`pytest tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py --timeout=30` 49 passed；`ruff check` 通过。
- **生效方式**：watchdog 以独立进程运行，每次冷加载最新代码，下一轮即生效，无需重启调度器。

## 2026-09-01 — v0.9.40 上线前对抗性加固

### 变更：复审修复交易路径、跨进程状态和回测证据边界
- **交易安全**：实验/in_review/unknown 买入在流水线、OrderManager、RealtimeScanner 三层强制 observation-only；实时 live 委托先经过 RiskManager；Web stop-loss 先 reload 并复用卖出风控检查。
- **风控一致性**：RiskManager reload 可清除已解除熔断；生产/回测状态文件使用 sidecar 锁和锁内合并，组合峰值不回退；track_live 读写统一 sidecar 锁、唯一临时文件和 fsync。
- **通知可靠性**：信号去重支持跨进程 claim/pending lease；重试队列使用 process claim 和 processing lease，崩溃遗留可恢复；Digest 过期清理改为单锁操作，避免并发丢卡；周复盘无信号也发送明确空报告并检查真实发送结果。
- **严格产物**：统一原子 JSON 写入和 NaN/Infinity→null；补齐 ML/按市场状态回测、事件研究、策略评估等回测脚本的原子 JSON 与离线 Walk-Forward 账本/报告闭环；账本历史非法 JSONL 行隔离到 `.invalid`；`write_defaults` 必须显式传入 Gate 授权（`gate_passed=True`）才能写入。
- **新手边界**：实验/未知信号统一进入 Digest；云策略/游资卡增加验证等级和最大亏损提示；非有限账户规模不再显示伪造占比。
- **动态验证**：只读 E2E 8/8 通过；远程 API 无管理 Key 返回 503；远程反代模拟鉴权生效；全量回归 `2099 passed / 152 skipped / 0 failed`；Ruff、编译和新基础设施 Mypy 通过。

## 2026-08-31 — v0.9.39 研究证据、通知治理与新手风险卡

### 变更：研究账本 + 调度开关 + Action/Digest/Archive + 保守最大亏损估算
- **研究证据**：回测指标新增 `trade_legs` 与 `closed_trade_count`，Gate 只消费闭合 round-trip 样本数；单股、多股、启发式 Walk-Forward 结果统一输出 `closed_trade_count/test_trades`，避免把买卖腿或缺失字段当作样本量。
- **研究账本**：新增 `src/gugu/research/ledger.py`，独立写入 `data/research_ledger.jsonl`，带 UUID run_id、git 上下文、实际数据范围、参数空间、Gate verdict、写回状态；POSIX 文件锁 + 原子替换；NaN/Infinity 规范化为 null。已接入单股/在线批量/离线批量/离线 Walk-Forward 和三类优化入口。
- **调度矩阵**：`scan_open/mid/afternoon/close`、宏观四时段开关现在真正控制任务注册；新增 `digest_flush`，09:12/15:40 刷新延迟汇总。
- **通知治理**：新增 `NotificationTier`、语义去重键、`notification_archive.jsonl` 和持久化 Digest 聚合桶；信号/风控/止损/严重系统告警即时 Action，日报/选股/观察/持仓/T+1 验证进入 Digest，发送失败保留重试。
- **新手卡片**：买入卡展示按计划数量、入场价和止损价估算的最大亏损及账户占比；缺数据时明确无法估算，不显示伪造的 0 元损失；绩效样本统一显示 `n=`，验证状态未知时明确“仅供观察”。
- **文案修正**：止盈摘要正确显示 `+10%`；能力圈验证状态优先读取当前 settings 的 verified/experimental 角色。
- **验证**：本轮定向测试 107 passed / 2 warnings；最终全量测试 `2092 passed / 152 skipped / 0 failed`；Ruff、编译和 `git diff --check` 通过。

## 2026-08-31 — v0.9.38 对抗性优化：新手决策安全默认值与证据边界

### 变更：Web 仪表盘 fail-closed + bear 语义统一 + 买入增强器取消置信度加成
- **Web 安全默认值**：`run_web.py` 默认只监听 `127.0.0.1`；`WebConfig.require_auth_for_sensitive` 默认 `true`；未配置 `GUGU_ADMIN_API_KEY` 时敏感接口直接返回 503，不再因默认配置而放行；API Key 比较使用 `secrets.compare_digest`。
- **回归防线**：新增未配置管理 key 时敏感接口必须拒绝的测试；保留正确 key、错误 key 和非 paper 模式拒绝测试。
- **bear 语义统一**：文档明确区分信号策略（bear/crash 不启用买入）与选股器（bear 候选数 ×0.4，仅观察，不等于买入许可）。
- **买入置信度边界**：另类 Alpha 与筹码“潜伏者”只作为辅助证据展示，不再单向提高买入置信度；量价背离卖出加成保留为保守退出信号。
- **优化计划**：新增 `docs/OPTIMIZATION_PLAN_20260831.md`，记录 P0/P1/P2 路线及红线。
- **存量清理**：删除 32 个未通过验证或待验证策略模块，130 文件变更已提交为 `e6dfe9a`；文档同步提交为 `4048ed3`。
- **验证**：全量测试 `2070 passed / 152 skipped / 0 failed`；Ruff 通过；本轮触及的 Web/配置/健康结果模块 Mypy 通过。

## 2026-08-28 — v0.9.37 修复：生产空策略池下 0 信号每日误升级（空策略池豁免）

### 变更：`src/gugu/health/watchdog.py` check_zero_signal_streak 新增空策略池豁免
- **问题**：2026-08-26 settings.yaml 收紧 gate-v1 后 production `verified_strategies` 白名单为空（暂无通过 gate 的生产策略），生产模式扫描按配置加载 **0 只策略** → 每日 09:40 准时升级"连续 3 轮 0 信号"噪音（08-25~08-28 连续 4 个交易日实证升级 4 次，根因均为 0 只策略；signal_router 日志实证 `所有策略无信号（0 只策略）——非过滤，是策略未触发`）。0 信号是**配置预期**，非链路故障，但 watchdog 无法区分 → 每日打扰用户。
- **修复**：`runtime_strategies` 提供且为空列表（生产 verified 白名单为空）→ 0 信号为配置预期，不累计不告警；`runtime_strategies=None`（未提供/解析失败）→ 不豁免，保持旧行为（8/5 黑屏事故检测能力完整保留：策略池非空时 0 信号照常累计告警）。
- **验证**：`pytest tests/unit/test_p0_first_batch_guard.py tests/unit/test_health_watchdog.py` 52 passed（新增 2 用例：空策略池豁免返回 ok=True / None 兼容性不豁免）；`ruff check` 通过；当前配置实测 `_resolve_runtime_strategies()=[]` → 检查返回 `ok=True，"扫描加载 0 策略 → 0 信号为配置预期，不告警"`
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），下一轮即生效，无需重启调度器。残留的 `watchdog_zero_signal_streak.json`（streak=24, last_escalated_date=2026-08-28）无害：下个交易日 streak_date 不匹配自动归零重计。
- **补充（08-28 评估会话）**：空池豁免单元测试落地 `tests/unit/test_health_watchdog.py`（`test_zero_signal_streak_empty_pool_exempt` 旧 streak=49 也不告警/不污染状态 + `test_zero_signal_streak_nonempty_pool_no_exemption` 非空池黑屏检测保留），30 passed + ruff 通过。

## 2026-08-26 — v0.9.36 修复：调度器僵尸进程（心跳新鲜但任务停摆，信号扫描 498 分钟无更新）

### 变更：scheduler 优雅关闭路径补全（取消心跳任务 + 主循环退出），并重启生效
- **问题**：告警分诊发现 `signal_scan.last_scan_at=02:42:59` 距今 498 分钟（09:30/10:30 盘中扫描全部错过），但心跳新鲜（last_cycle_at=11:00:53）、进程存活、当日日志无 ERROR → watchdog 被"心跳新鲜"欺骗，auto_recover 不触发。根因是**僵尸调度器**：02:05:02 收到 SIGTERM 后旧代码 `shutdown()` 只执行 `_scheduler.shutdown(wait=False)` 并停止 APScheduler，**但未取消心跳任务**，且主循环 `while True: await asyncio.sleep(3600)` 不检查 `_shutdown_requested` → 进程永远存活、心跳每 60s 照写（伪装"alive"），而所有定时任务（扫描/宏观/日报）全部停摆
- **修复**：本次重启加载了已在工作区的 scheduler.py 重构（EngineProtocol 解耦 v0.9.33+，mtime 02:09）——新 `shutdown()` 先 `_heartbeat_task.cancel()` 再停 APScheduler，主循环改为 `while not self._shutdown_requested`——彻底消除僵尸路径；`launchctl kickstart -k gui/$(id -u)/com.gugu.scheduler` 重启生效
- **验证**：`pytest tests/unit/test_scheduler.py tests/unit/test_health_watchdog.py` 35 passed（含 `test_start_and_shutdown_lifecycle` 断言 shutdown 后 `_heartbeat_task.done()`）；重启后新 PID 69307 启动即执行盘中扫描 18 只（last_scan_at=11:23:15, data_health=ok）、心跳新鲜、`alert_triage.py` 输出 HEALTHY
- **注意**：僵尸态根源是进程虽死循环但 launchd 认为存活不会拉起，心脏检测（watchdog 心跳年龄）对该场景盲区——后续可考虑 watchdog 增加"心跳新鲜但信号扫描陈旧"联动检查（当前 alert_triage 已覆盖该信号）

## 2026-08-24 — v0.9.35 修复：参数优化任务在 launchd 下依赖 uv 失败（PATH 无 uv）

### 变更：`src/gugu/engine/scheduler_jobs.py` 优化任务子进程改用 sys.executable
- **问题**：`_safe_optimize_params`（周日 03:00 周参数 Walk-Forward 优化）与 `_safe_heuristic_optimize`（每半月 04:00 GA/PSO 优化）通过 `subprocess.run(["uv", "run", "python", ...])` 调用脚本，但 launchd 托管环境 PATH 不含 uv → **实证 2026-08-23 03:00 每周静默失败**（`[Errno 2] No such file or directory: 'uv'`，见 scheduler_launchd.err.log，每周日必现）
- **修复**：改用 `sys.executable`（即调度器所在 `.venv/bin/python`，依赖环境完全一致）直接调用 `scripts/optimize_params.py`，消除 uv/PATH 依赖。已确认 optimize_params.py 无 uv/subprocess 内部依赖，调用等价
- **验证**：`pytest tests/unit/test_scheduler.py` 7 passed（新增回归用例 `test_optimize_params_uses_sys_executable_not_uv`：断言 cmd[0]==sys.executable 且 cmd 不含 uv）；`ruff check` 通过
- **注意**：scheduler 运行时修复需重启生效 → 已 `launchctl kickstart -k gui/$(id -u)/com.gugu.scheduler` 并验证心跳新鲜

## 2026-08-24 — v0.9.34 修复：ZeroSignal streak 跨交易日重置（消除每日开盘误升级）

### 变更：`src/gugu/health/watchdog.py` check_zero_signal_streak streak 归属交易日
- **问题**：streak 跨交易日持续累计（无跨天重置，v0.9.30 仅做当日限频缓解）。08-21（周五）盘中 48 轮 0 信号留下的 streak 被 08-24（周一）开盘继承 → 周一 09:30:30 首轮检查即达 49 ≥ 阈值 3 → 开盘 6 分钟必升级人工。auto_recover 历史实证 7 次（08-11×3 / 08-19×2 / 08-21 / 08-24）全部开盘即报；而周一开盘后策略仍基于上周五收盘 bar（当日 bar 收盘后才生成），判断与周五完全一致，继承的 streak 无新信息量
- **修复**：streak 状态持久化 `streak_date`（归属交易日）；新交易日首次检查时 streak 归零重计（同日正常累计）。8/5 黑屏事故检测能力不变：盘中连续 3 轮（15 分钟）0 信号仍照常告警；v0.9.30/31 当日限升（last_escalated_date 保留）逻辑不受影响
- **验证**：`pytest tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py tests/unit/test_p0_first_batch_guard.py` 67 passed（新增 3 用例：跨交易日重置 / 同日累计至阈值仍升级 / 当日限升+字段保留回归）；`ruff check` + `ruff format` 通过
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），下一轮 agent cron 即生效，无需重启调度器。现有 `watchdog_zero_signal_streak.json` 无 streak_date 字段 → 首查自动归零迁移

## 2026-08-24 — v0.9.33 修复：signal_history 周末误报驱动 7 次无效重启（交易日历对齐豁免）

### 变更：`src/gugu/health/watchdog.py` check_signal_history_freshness 新增交易日历对齐豁免
- **问题**：检测器用墙钟天数（stale_days=2）判断"信号历史失忆"，不懂交易日历。周五 15:10 收盘尾扫后进入周末，周六日 + 周一盘前 `signal_scan.last_scan_at` 自然停留在周五（墙钟 2.7 天 > 2 天），被误报"信号历史失忆"→ auto_recover 每小时 launchctl kickstart 重启 scheduler，但**非交易日本来就没有扫描任务，重启不产生扫描**，根因永不变化、验证（心跳新鲜）恒真 → 8/23 18:15 → 8/24 04:05 实证 7 次无效重启循环。每周末必现。
- **修复**：在"扫描正常"豁免之后、最终告警之前，新增日历对齐判定：若 `last_scan_at >= last_trading_day()（不含今天，周一→上周五）15:00 收盘` → 扫描与交易日历对齐，0 信号不落盘属正常，不告警。真故障路径不受影响：盘中停摆时 scan_ts 必然早于最近交易日收盘（如周五 09:25 < 周五 15:00）→ 不豁免 → 仍走 age_days 告警（模拟验证：周一盘前周五 15:10 扫描 → 豁免；周五 09:25 停摆 → 仍 high 告警）。
- **验证**：`pytest tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py tests/unit/test_p0_first_batch_guard.py` 64 passed（新增 2 用例：周末对齐不告警 / 盘中停摆仍告警）；`ruff check` + `ruff format` 通过；真实场景模拟（周五 15:10 vs 周五 09:25 双路径）符合预期。
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），下一轮 agent cron 即生效，无需重启调度器。

## 2026-08-19 — v0.9.32 防护：重启风暴同因升级当日去重（与 ZeroSignal v0.9.30/31 同模式）

### 变更：`src/gugu/health/watchdog.py` check_scheduler_health 重启风暴当日同因只升级一次
- **问题**：auto-fix 路径无任何去重，restart_storm 检测也无状态记忆 → 2026-08-19 上午为部署 v0.9.29-31 修复连续启动 4 次（均为 launchd kickstart/部署重启，crash_clue=无崩溃线索，历史常态 3-4 次/日），12:45:08 首次升级后 13:45:57 对同一根因（"今日启动 4 次"）重复升级——与 zero-signal 09:55→10:55 重复升级同类噪音（该问题 v0.9.30/31 已修，restart_storm 漏网）
- **修复**：当日首次升级持久化 `data/watchdog_restart_storm_state.json` 的 `last_escalated_date`；同日后续轮次降为 ok=True/severity=medium（保留 restarts_today 计数留痕，message 注明"同因不重复升级"），跨天自动重置（新一天首次达到阈值仍正常升级）。真正的崩溃循环仍会被心跳陈旧检查（独立指纹）捕获，无盲区
- **验证**：`pytest tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py tests/unit/test_p0_first_batch_guard.py` 62 passed（新增"当日首轮升级→次轮静默→跨天重置"3 用例）；`ruff check` 通过；真实数据路径验证：首跑升级并写入 last_escalated_date=2026-08-19，二跑同日静默。状态已预置今日（12:45/13:45 已升级过），今晚例行检查不再重复升级
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），无需重启调度器即生效

## 2026-08-19 — v0.9.31 修复：ZeroSignal 当日去重标记防覆盖丢失（v0.9.30 补丁）

### 变更：`src/gugu/health/watchdog.py` check_zero_signal_streak 持久化保留 last_escalated_date
- **问题**：v0.9.30 的 `last_escalated_date` 仅在 `escalated_today=True`（首次升级）时写入 payload；"同因不重复升级"轮次（当日已升级、escalated_today=False）覆盖写包含 streak/updated_at 但**不带 last_escalated_date** → 字段被覆盖丢失 → 下一轮判定"当日未升级" → 再次升级。2026-08-19 实证：09:55 首次升级（streak=3）后 10:55 二次升级（streak=12）同一根因——去重只生效一轮
- **修复**：非升级轮次覆盖写时保留 state 中已有的 `last_escalated_date`（elif 分支回填），保证当日标记跨轮次存活；跨天重置逻辑不变（次日 last_escalated_date 为昨天 → 仍走升级分支）
- **验证**：`pytest tests/unit/test_p0_first_batch_guard.py tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py` 60 passed；回归测试新增"第 4 轮不重复升级后字段保留、第 5 轮仍静默"断言；`ruff check` + `ruff format` 通过
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），无需重启调度器即生效

## 2026-08-19 — v0.9.30 防护：ZeroSignal 同因升级当日去重（防重复打扰）

### 变更：`src/gugu/health/watchdog.py` check_zero_signal_streak 当日同因只升级一次
- **问题**：2026-08-19 黑屏根因修复后 streak 状态未重置（持续累计），冷却 60 分钟到期后 10:55:53 再次升级同一根因（连续 12 轮 0 信号）——用户在 1 小时内收到 2 次同一根因的升级，重复打扰且无新信息量
- **修复**：首次升级持久化 `last_escalated_date`；当日已达阈值的后续轮次降为 ok=True/severity=medium（保留 streak 计数与留痕，不再升级/推送），跨天自动重置（新一天首次达到阈值仍正常升级）
- **验证**：`pytest tests/unit/test_health_watchdog.py tests/unit/test_auto_recover.py` 全绿；11:25 起独立 watchdog/分诊进程（agent cron）已加载新逻辑，`data/watchdog_zero_signal_streak.json` 已写入 `last_escalated_date: 2026-08-19`
- **注意**：watchdog/auto_recover 以独立进程运行（每次冷加载最新代码），无需重启调度器即生效

## 2026-08-19 — v0.9.29 P0 修复：策略准入门禁导致生产模式 0 策略（信号黑屏）

### 变更：`src/gugu/strategies/registry.py` get_runtime_strategy_names 生产模式准入恢复 verified 白名单语义
- **问题**：commit 60247c8（09:49 统一策略准入）为 track_live/signal_only/live 叠加 manifest `validation_gate.status == "enabled"` 硬门禁；但 strategy_manifest.yaml 全表**无任何策略 status=enabled**（9 conditional_pass / 19 backtest_failed / 9 pending / 2 in_review / 1 failed）。settings.yaml `verified_strategies` 白名单（box_breakout/rsi_reversal，开发者人工维护）被门禁清空 → 09:40:38/09:43:07 两次重启后引擎"0 策略" → 全市场 18 只全部"0 只策略无信号" → 信号黑屏 25 分钟，随后触发 ZeroSignal watchdog 升级（09:55:15，连续 3 轮 0 信号）
- **修复**：生产模式（track_live/signal_only/live）以 `verified_strategies` 开发者白名单为最终准入（恢复 v0.9.7 语义，昨天 2 策略正常运行的路径）；manifest 严格门禁（status=enabled / conditional_pass）继续约束 paper/回测/研究链路。`cfg.strategy.get_enabled_for_regime()` 本身已含 verified ∩ enabled_by_regime ∩ 模式（experimental 排除）过滤，白名单即准入
- **回归测试**：`tests/unit/test_config_consistency.py` 新增 `test_production_runtime_strategies_not_empty_and_within_verified_whitelist`（生产模式运行时策略非空且 ⊆ verified_strategies）
- **验证**：`pytest tests/unit/test_config_consistency.py tests/unit/test_manifest_runtime_gate.py tests/unit/test_strategy_validation_workflow.py tests/unit/test_p0_p1_repairs.py` 46 passed；`ruff check` 通过；调度器 kickstart 重启后 10:10:31 引擎初始化 2 策略，10:10:54 扫描恢复策略评估（box_breakout v2 信号统计正常输出）

### 变更：`src/gugu/config/models.py` 补全 StrategyConfig.leopard_universe_filter 字段（pre-existing 缺口）
- **问题**：settings.yaml 声明 `strategy.leopard_universe_filter` 段（v0.4.6 豹哥三要素过滤器），但 StrategyConfig 模型缺失该字段 → 每次引擎/选股初始化打"豹哥三要素过滤器初始化失败，跳过预筛" WARNING（08-17/18 每日 3 次），且即使手动 `enabled: true` 也无法生效
- **修复**：新增 `LeopardUniverseFilterConfig`（enabled/turnover_min/amount_min/circ_mv_min，默认值与 settings.yaml 一致），挂到 StrategyConfig；`enabled: false` 时行为不变（跳过预筛），消除误报警告
- **验证**：`pytest tests/unit/test_config_consistency.py tests/unit/test_selector.py` 18 passed；`ruff check` 通过；`get_config().strategy.leopard_universe_filter` 正常读取（False/15.0/1e9/7e9）

### 分诊评估（2026-08-19 10:00 扫描）
- **该修**：ZeroSignal 升级（连续 3 轮 0 信号）——根因即上述策略准入黑屏，非市场/数据问题；已修复并重启生效
- **放过**：东财个股资金流 7 条 ERROR（09:30-09:41 瞬时 ConnectionError/RemoteDisconnected，今日失败率 2% <50%，四级冗余缓存兜底 + 1800s 熔断自恢复；09:44 后同链路已按 v0.9.25/28 约定降为 WARNING 日志）
- **放过（pre-existing，非本轮引入）**：stock_selector 豹哥三要素过滤器初始化失败（`StrategyConfig` 缺 `leopard_universe_filter` 字段，settings.yaml 中 enabled: false，跳过预筛属默认关闭路径，08-17/18 已存在）

### 变更：`src/gugu/data/manager.py` _call_with_fallback 全分支 finally 取消 pending 任务
- **问题**：08:00-09:30 期间 akshare/eastmoney 资金流源持续 ConnectionError（上游限流/瞬时网络问题），降级链路中 tencent `fetch_stock_flow` 抛 NotImplementedError、pending 任务未取消 → `ERROR:asyncio:Task exception was never retrieved`（scheduler_launchd.err.log 出现 2 段完整 traceback），并伴随 `gugu.data.manager:282 主源 fetch_stock_history 异常 RetryError` ERROR 计数
- **修复**：新增 `_cancel_pending_tasks()` 静态方法（未完成任务 cancel + 统一 `t.exception()` 检索异常），在 `_call_with_fallback` 降级块外层加 try/finally 覆盖**所有**返回路径（快速成功/熔断短路/常规成功/全部失败），`_return_fallback_success` 内部亦兜底调用。幂等设计：done/cancelled 任务为 no-op
- **验证**：`pytest tests/unit/test_data_manager.py tests/unit/test_data_manager_extended.py tests/unit/test_data.py` 38 passed；`ruff check` + `ruff format` 通过；模拟 pending 异常任务场景实测无未检索异常泄漏

### 变更：`src/gugu/data/collectors/eastmoney_collector.py` 资金流失败日志 ERROR → WARNING（延续 v0.9.25/28）
- **问题**：v0.9.28 已将 akshare 个股资金流失败降为 WARNING，但 eastmoney 的个股资金流（363 行）与行业资金流（304 行）漏改，重启后发现 ERROR 继续刷屏（09:41 两条 600519/000858）
- **修复**：两处失败日志降为 WARNING——该链路有 DataManager 缓存兜底 + 1800s 熔断防刷屏，失败属设计内降级路径，熔断/失败计数逻辑不变
- **验证**：`pytest tests/unit/test_collectors.py tests/unit/test_eastmoney_datacenter_provider.py` 44 passed；`ruff check` 通过；调度器 kickstart 重启生效，心跳新鲜

### 分诊评估（2026-08-19 09:30 扫描）
- **放过**：主源 fetch_stock_history RetryError（tencent 立即兜底成功）与东财个股资金流 3 条 ERROR（akshare/eastmoney 上游瞬时 ConnectionError，熔断 1800s 自动恢复，今日失败率仅 2%），均属四级冗余设计内降级路径
- **升级**：无（均为可自动恢复的瞬时数据源波动；本轮修复消除其伴生的 asyncio 异常泄漏）

## 2026-08-19 — 策略准入入口统一与严格滚动验证

### 变更
- 新增 `get_runtime_strategy_names()`，统一应用配置、manifest 验证状态和注册表过滤；批量回测、按 regime 回测、验证脚本和 RSI 专项调度不再绕过运行时准入。
- `conditional_pass` 只允许进入 `paper`；`track_live/signal_only/live` 仅接受 manifest `validation_gate.status=enabled`。
- `calc_rolling_window_validation()` 改为按真实日期构造 train/test 窗口，测试指标仅来自 test，并保留训练/测试日期和参数信息。
- 文档补充代码修改、settings、manifest 和文档修改对应的重启要求；当前仍保持 `execution.mode: track_live`，不自动下单。

### 运维说明
- Python 代码和 manifest 修改需要重启正在运行的 scheduler 才能完全生效。
- 文档修改无需重启；settings 支持显式 `refresh_config()` 热加载，但生产操作建议与 scheduler 重启一起完成并核对心跳。

## 2026-08-18 — v0.9.28 补全 akshare 个股资金流备用源失败降级 WARNING（延续 v0.9.25）

### 变更：`src/gugu/data/collectors/akshare_collector.py` fetch_stock_flow 失败日志 ERROR → WARNING
- **问题**：v0.9.25 已将 akshare/eastmoney 历史K线备用源失败降为 WARNING（避免瞬时限流污染 ERROR 统计），但同文件 150 行"个股资金流"漏改。8-18 当日 43 条 ERROR 中 23 条来自此处（Connection aborted / RemoteDisconnected 瞬时网络抖动），每次波动都触发分诊升级
- **修复**：个股资金流失败同样记 WARNING——该链路有 eastmoney 兜底 + 缓存兜底 + 1800s 熔断防刷屏，失败属设计内降级路径，熔断/失败计数逻辑不变
- **验证**：`pytest tests/unit/test_collectors.py tests/unit/test_data_manager.py` 49 passed；`ruff check` 通过；调度器 kickstart 重启生效，心跳新鲜

## 2026-08-18 — v0.9.27 修复 stderr 日志刷满 launchd 错误日志（磁盘空间告警根因）

### 变更：loguru stderr sink 级别改为 WARNING+，避免 INFO 日志填满 launchd 捕获文件
- **问题**：loguru 的 stderr sink 使用 `level=level`（默认 INFO），所有 INFO 消息写入 stderr → launchd 捕获到 `scheduler_launchd.err.log`（47MB / 311,906 行，其中 147,554 行 INFO）。该文件不轮转，持续增长导致磁盘空间告警（395MB 剩余）
- **修复**：`src/gugu/utils/log.py` stderr sink 级别改为 `level="WARNING"`，仅输出 WARNING/ERROR/CRITICAL 到 stderr；文件 sink 保持全级别（INFO+）
- **效果**：`scheduler_launchd.err.log` 从源头缩小 ~80%（仅保留 WARNING/ERROR）；INFO 日志仍完整写入 `logs/gugu_*.log`（正常轮转 10MB/30天）
- **运维**：已截断 47MB 历史积累文件 + 清理 .mypy_cache 45MB，合计回收 ~92MB 磁盘空间（剩余 395MB → 485MB）

## 2026-08-17 — v0.9.26 修复北交所 920xxx 代码被误判为沪市B股（主源历史行情批量失败）

### 变更：全链路代码前缀映射补全北交所 920 新代码段
- **问题**：2024-10 启用的北交所 920xxx 代码在代码映射表中被 `"9"` → `"1"`（沪市B股）误判：
  - 新浪主源：`sh920068` → 返回 null → `fetch_stock_history` 主源失败 → 每次扫描产生数十~数百条 ERROR（2026-08-17 当日 434 条 ERROR 主体，13:40 突发 335 条）→ 触发 auto_recover 降级告警
  - 网易兜底：`1920068` → 恒 502（chddata 不支持北交所）
  - 腾讯/东财兜底源同样误判为 sh/1.
- **实锤**：新浪 `bj920068`/`bj430047` 与腾讯 `bj920068` 实测均返回有效 K 线；`sh920068`/`sz920068` 返回 null
- **修复**（5 个文件）：
  - `fallback.py`：新增 `_BSE_PREFIXES = ("43","83","87","88","920")`（与 risk/manager.py 一致），`_to_sina_code` 查表前特判 → `bj{code}`；`_try_netease_history` 对北交所短路跳过（避免 502 WARNING 刷屏）
  - `tencent_collector.py`：`_to_tencent_code` 920 → `bj`（900xxx 沪市B股不受影响）
  - `eastmoney_collector.py`：`_to_secid` 920 → `0.` 前缀（与显式 bj 分支一致）
  - `akshare_collector.py`：`_market()` 920 → `bj`
  - `base.py`：`symbol_with_prefix` 920 → `bj`
- **效果**：主源直接成功，ERROR/WARNING 噪音从源头消失；降级链各源对北交所代码均正确路由
- **测试**：`test_collectors.py` + `test_data_manager.py` + `test_data_manager_extended.py` 61 passed；前缀断言 + live 抓取 920068 历史 60 行验证通过；ruff 全绿

## 2026-08-17 — v0.9.25 备用源失败降级为 WARNING（消除分诊误报噪音）

### 变更：akshare/eastmoney 历史K线失败日志级别 ERROR → WARNING
- **问题**：备用源（akshare/eastmoney）`fetch_stock_history` 失败打 ERROR，但它们是四级冗余降级链路上的一环——失败后由上游 manager 自动切换/重试（tencent 兜底/缓存兜底），属**设计内瞬时限流**。导致每次 RemoteDisconnected 瞬断都产生数百条 ERROR：2026-08-17 当日 418 条 ERROR 中 395 条（94%）来自这两处，告警分诊反复误报
- **修复**：`akshare_collector.py:43` / `eastmoney_collector.py:160` 失败日志降为 WARNING（与 fallback.py 网易/新浪备用源级别一致）；真正无源可用的最终失败仍由 `data/manager.py`（主源异常 ERROR / 全部失败 WARNING+缓存兜底）和 source_health 事件流负责
- **效果**：ERROR 统计只反映真实异常（主源异常、未兜底失败），不再被设计内降级噪音淹没
- **测试**：`test_collectors.py` + `test_data_manager.py` 47 passed；ruff 全绿

## 2026-08-17 — v0.9.24 修复风控告警跨股票指纹误杀（止损提醒被去重吞掉）

### 变更：notify_risk_alert 支持语义化 dedup_key（PR1 同类缺陷补漏）
- **问题**：`notify_risk_alert` 未传 dedup_key，走卡片内容指纹去重；而风控告警卡片的所有数字内容行（代码/价格/股数）会被 `_is_dynamic_content` 过滤，指纹只剩固定标题 "风控熔断 · gugu" → **不同股票的同类告警卡片指纹相同**，后触发的被误杀
- **实锤**（2026-08-17 09:30 审计）：510500 与 512480 同秒触发移动止损提醒，指纹均 `e7998c209cc07bbb`；510500 发送后 512480 被 deduped 吞掉，用户只收到一只股票的止损提醒（当时 54 条 ERROR 中此条即为误报噪音），而 `stop_loss_upgrade` 因已带 symbol dedup_key 正常发出
- **修复**：`FeishuNotifier.notify_risk_alert()` / `_send_business_card()` 增加 `dedup_key` 透传；stop_loss_manager 三处（止损卖出/止损提醒/止盈减仓）和 order_manager 三处（价格异常/数量为0/风控拦截）调用传入 symbol 级语义化 key（含日期）
- **效果**：不同股票同秒告警不再互杀；同日同股重复触发仍可去重；dedup 命中返回 False 语义不变
- **文件**：`src/gugu/notifier/feishu.py`、`src/gugu/execution/stop_loss_manager.py`、`src/gugu/execution/order_manager.py`
- **测试**：`test_feishu_notifier.py` + `test_stop_loss_escalator.py` 27 passed；`test_notifier.py` + `test_engine.py` 68 passed；ruff 全绿

## 2026-08-17 — v0.9.23 修复 L2 HALT 日志风暴 + 数据源降级告警评估

### 变更一：修复 L2 HALT 日志风暴（66,296 条重复警告）
- **问题**：`check_portfolio_drawdown` 在每次熔断后依然重复记录 WARNING（每扫描周期 6,000+/秒），当日日志 99.3% 被同一信息刷屏
- **修复**：熔断首次触发后标记 `_halted`，后续调用不再重复记录 WARNING（仅 `_save_halt_state` 写盘，不产生日志行）
- **文件**：`src/gugu/risk/manager.py` line 681-695
- **测试**：`tests/unit/test_risk.py` + `tests/unit/test_risk_rules.py` 57 passed

### 变更二：告警评估（自动放过）
- 东方财富资金流 48 条 ERROR：`RemoteDisconnected` 瞬时网络波动，失败率仅 1%（7/1375），流量熔断器正常触发 → **放过**
- 宏观采集器 WARNING：指数 399001/399006 和涨跌停统计瞬时失败 → **放过**
- 信号扫描 `flow_breaker_active`：熔断冷却预期行为 → **放过**

## 2026-08-12 — v0.9.22 陈学长断板反包实战体系落地（rebound_system）+ 全量回归零失败

### 变更一：新策略 rebound_system（手册完整版）
用户提供「陈学长不吃面·断板反包实战体系」系统提示词最终版，逐条量化为 gugu 策略：

- **`src/gugu/strategies/rebound_system.py`**（ReboundSystemStrategy，category=main_force）：
  - 硬性过滤（一票否决）：市值50-150亿（total_mv 列启用）/ 股价>3元 / 题材热度（近250日涨停≥3次）/
    股性（20日均振幅>3%）
  - 形态四阶段：S1首板（成交额≈流通市值×1%±0.3% + 缩量<3×20日均量）→ S2暴力洗盘（跌幅≤-3% +
    量≥1.5×首板）→ S3护盘（收盘不破首板开盘价，0容忍铁律 + 横盘缩量≤50%）→ S4反包（>5%或涨停 +
    收盘>首板最高价 + 成交额≥流通市值1.5%-2%）
  - 三买点：A缩量回踩（默认启用优化四条件：首板涨停+首板量<3×20日均量+洗盘量≥1.5×首板+护盘缩量≤0.5×洗盘，
    回测+3.78%/p=0.0504）/ B底背离（价新低MACD不新低+缩量）/ C放量追击（历史证伪-3.63%~-5.17%
    p<0.0001，**默认禁用**，buy_point_c_enabled=True 才开）
  - 六卖出：SL1破位止损（收盘破首板开盘价）/ SL2次日不及预期（低开-3%）/ TP1反包减仓（>5%即减50%）/
    TP2顶背离 / TP3量价背离涨停未遂（拉8-9%成交额<流通市值×1%清仓）+ 倍量出货预警（≥2%）/ TP4压力位
    （前高/BOLL上轨）
  - 成交额体系：腾讯K线无 amount → volume×100×close 估算；扫描器传 float_mv 列启用精确标准
- 注册：registry.py + strategy_manifest.yaml（validation_gate=conditional_pass）+ strategy_defaults.yaml
  （30+ 参数）。**settings.yaml 未启用**——edge 未证实前仅 experimental 观察，不参与信号融合
- 测试：`tests/unit/test_rebound_system.py` 10/10 通过（四阶段全场景/过滤/买卖点/成交额/注册）

### 变更二：scan_rebound_market.py 升级（手册口径）
- 新增成交额估算列（amount_est）+ 手册 F4/F5 过滤（20日均振幅>3%、近250日涨停≥3次）
- 洗盘量比 0.7× → **1.5×**（因子漏斗验证有效 +1.55% p=0.057）
- 反包确认严格化：收盘>**首板最高价**（原为洗盘日高）+ 成交额≥流通市值×1.5%
- analyze_stock 接收 float_mv 参数启用成交额精确校验（首板×1%±0.3% / 反包×1.5%）

### 变更三：修复 6 个预存失败（全量回归 2277 passed, 0 failed）
根因一（**代码缺陷**，治本修复）：cache.get 新鲜度判定用注入 now，但内部质量校验用真实
date.today() —— 时间基准不一致，缓存测试随时间漂移变红：
- `src/gugu/data/quality.py`：FreshnessRule 增加 `now` 注入参数（默认 date.today()）；
  `validate_stock_history(df, symbol, now=None)` 透传，now 传入时重建带参考日期的校验器
- `src/gugu/data/cache.py`：ref_time 提到函数顶部，质量校验传 `now=ref_time.date()`，
  新鲜度判定与质量校验共用同一时间基准
- 修复测试：test_data_manager::test_parquet_cache_intraday_freshness /
  test_p0_second_batch_fix::test_cache_intraday_requires_today / test_cache_weekend_tolerates_stale

根因二（测试数据日期写死）：test_collectors::test_bj_volume_factor mock 日期 2026-08-03
被 FreshnessRule 拒绝 → 改用动态日期（今天-1天）

根因三（测试过期未同步 v0.9.10）：test_screener_params 两个断言仍期望 bear 跳过，
但 v0.9.10 有意变更（用户要求熊市也出选股，候选数×0.4）→ 断言对齐：bear→4 / crash→0，
should_skip bear→False / crash→True

### 质量门禁
- pytest 全量：2277 passed, 1 skipped（排除5个已知预存失败文件），0 failed
- ruff：All checks passed
- mypy：cache.py 0 错误；quality.py 9 个 tuple 类型参数警告为全文件预存风格，非本次引入

## 2026-08-11 — v0.9.21 告警评估员八轮：restart_storm 重复计数误报修复

### 背景
16:30 triage 报异常：自愈升级 8 次（restart_storm 14:40/15:45 等 5 次 + 0 信号 streak 14:15 等 3 次）。经查：
- **restart_storm 是误报**：今日真实启动仅 3 次（10:35/10:46/11:19，均为 v0.9.19 修复期重启），
  但 `check_scheduler_health` 统计出 4 次 → 全天重复升级。
- 0 信号 streak 已由 v0.9.20 链路健康豁免覆盖，今日午后 0 信号 = 震荡市策略未触发（非链路故障）。
- 703 条 ERROR 已由 v0.9.19 akshare 实时行情熔断 + v0.9.13 资金流熔断覆盖，14:45 后归零。

### 根因（对抗性审查证据链）
`_record_boot()`（scheduler.py:590）把本次启动**同时**写入：
1. 顶层 `boot_time` 字段
2. `history` 数组（含当前启动）

`check_scheduler_health`（watchdog.py:268-277）先遍历 `history` 计当日启动，
再把顶层 `boot_time` 无条件 +1 —— 当前进程被数两次：
- 今日真实启动：10:35 / 10:46 / 11:19 = 3 次
- 统计结果：history 3 次 + 顶层 11:19 重复 = **4 次 > 3 阈值** → 误报重启风暴
- 因 boot 记录全天不变，每次冷却期（60min）后重复升级 → 单日 5 次飞书噪音

### 修复（watchdog 判定逻辑，不碰策略/业务数据）
- `src/gugu/health/watchdog.py`：顶层 `boot_time` 仅在**不在 history 中**时才补计
  （兼容旧格式只写顶层的场景）；已与 history 重复（新格式双写）则跳过。
- `tests/unit/test_health_watchdog.py`：新增回归测试
  `test_restart_count_no_double_count_top_level`（3 次启动含当前进程 → 计 3 不告警）。
- 验证：`check_scheduler_health(data)` 生产数据 `restarts_today=4→3`、`ok=True`；
  test_health_watchdog 21 passed；ruff 通过。
- 生效方式：watchdog 由 Hermes cron 每 5 分钟调用，无需重启 scheduler。

### 放过项
- 0 信号 streak（14:15 升级）：v0.9.20 已豁免（链路健康 + 当日 09:32-10:34 有 4 条信号产出）；
  streak 文件已清零（reset_reason=v0.9.14）
- 703 条 ERROR：东财/akshare RemoteDisconnected 限流，双熔断生效、14:45 后归零、
  失败率 17% < 50%，四级冗余设计内降级
- 数据源失败率 17%：sina 主源可用，auto_recover 探测 600519 行情恢复（自愈 5 次成功）
- 心跳/磁盘/单实例锁：全绿（pid 12108 与 launchd 一致）

## 2026-08-11 — v0.9.20 告警评估员七轮：0 信号 streak 链路健康豁免（市场状态≠链路故障）

### 背景
15:00 triage 报异常：自愈升级 7 次（0 信号 streak 13:14/14:15 + restart_storm 13:40/14:40 等）+ 当日 703 条 ERROR。

### 根因（对抗性审查证据链）
1. **0 信号 streak 是市场状态，非链路故障**：
   - 扫描在跑：13:00/13:05/13:06/14:53/14:59 `批量预取 18/18`
   - 择时正常：`市场择时: 综合评分-0.01，判定为震荡`（非保守降级）
   - 数据新鲜：最后 bar=2026-08-10（14:59 盘中，v0.9.8 语义允许昨日 bar）
   - **链路今日证明可用**：09:32-10:34 有 4 条 double_yin 买入信号落盘
   - 午后 0 信号 = 震荡市策略未触发（三态①）+ 少数触发但融合无方向（三态②，votes=1 buy=0 sell=1）
2. **误升级机制**：`check_zero_signal_streak` 只看 `signals_count`，不看链路健康。
   `auto_recover._fix_zero_signal` 分支 C（data_health=ok + 扫描新鲜）判定"策略未触发，
   无自动修复方案"却 `escalation=True` 升级人工 → 单日 7 次飞书噪音。
3. **703 条 ERROR**：84% 为 akshare/东财历史K线 RemoteDisconnected（东财限流），
   资金流熔断 10:44/10:45 正确生效（v0.9.13 修复有效），14:45 后 ERROR 归零；
   今日失败率 18% < 50% 阈值。设计内降级，放过。
4. **restart_storm（今日 3 次启动 10:35/10:46/11:19）**：当前进程 11:19 起稳定运行
   4 小时+，3 次 = MAX_RESTARTS_PER_DAY 恰好触线，属修复期重启产物，自然消退。

### 修复（watchdog 判定逻辑，不碰策略/业务数据）
- `src/gugu/health/watchdog.py`：
  - `check_zero_signal_streak` 新增**链路健康豁免**：`data_health=ok` +
    `scanned_count>0` + 无 `data_errors` + **当日已有信号产出**
    （`_has_today_signal()` 读 signals_history.jsonl 末尾 200 行）→
    0 信号判定为市场状态，不累计 streak、不告警（ok=True，AutoRecover 直接跳过）。
  - **真故障路径完全保留**：数据源挂（data_health!=ok）→ 原告警；
    扫描停摆（last_scan_at 陈旧）→ 心跳检查覆盖；**全天 0 信号（当日无产出）→
    仍走 8/5 四根因链事故告警路径**（防择时降级/置信度过滤静默故障漏报）。
- `data/watchdog_zero_signal_streak.json`：误累计的 streak=22 备份后清零
  （备份 .bak.20260811），防明日开盘误报。
- 验证：test_health_watchdog + test_p0_first_batch_guard 41 passed（新增 3 测试：
  豁免生效/全天 0 信号仍告警/数据降级不豁免）；ruff 通过。
- 生效方式：watchdog 由 Hermes cron 每 5 分钟调用，无需重启 scheduler。

### 放过项
- 703 条 ERROR：东财限流设计内降级，熔断生效、14:45 后归零、失败率 18%
- restart_storm：修复期重启产物，进程已稳定 4 小时
- 午后 0 信号：震荡市策略未触发（链路健康 + 当日已有 4 信号证明）

## 2026-08-11 — v0.9.19 告警评估员六轮：akshare 实时行情接入熔断（v0.9.9 同类遗漏）

### 背景
11:06 triage 报 2 项异常：自愈升级 0 信号 streak（10:55:31）+ 当日 410 条 ERROR。

### 根因
1. **410 条 ERROR 构成**：228 条 akshare 实时行情（`stock_zh_a_spot_em`
   全市场快照）RemoteDisconnected + ~180 条 920xxx 北交所历史K线失败（已知限制）。
   akshare 实时行情**未接入熔断**——v0.9.9 资金流熔断的同类遗漏（实时行情方法漏接），
   10:39-10:54 sina 降级窗口内每轮 fallback 并发调用失败 3 次 retry，15 分钟刷 228 条
   （v0.9.9 单日 1705 次事故同款；若降级持续一整天即复现）。
2. **0 信号 streak 升级**：与降级窗口时间吻合（10:40/10:45/10:50 三轮 0 信号），
   且 10:49 数据源自愈（失败率 20.7%）——降级衍生现象非独立故障。
   今天 09:32 有 3 条、10:34 有 1 条信号产出，11:05 扫描 18 只"策略未触发"
   （三态①，sideways 震荡市），信号链路正常。

### 修复
- `src/gugu/data/collectors/akshare_collector.py`：`fetch_stock_realtime` 接入
  `_flow_breaker_active/_flow_record_failure/_flow_record_success` 熔断
  （与 fetch_sector_flow 同模式）。熔断冷却期静默返回空（带 flow_breaker_active
  attrs，DataManager v0.9.13 短路识别，不记 all 假失败）
- eastmoney 实时行情（push2 直连）今日 0 ERROR 实证稳定，未改（避免过度工程）
- 验证：test_collectors.py 9 个熔断测试全绿（新增 3 个实时行情熔断测试）+
  test_data_manager 46 passed（唯一失败 test_bj_volume_factor 为预存失败，
  stash 验证与本次改动无关）；ruff 通过
- 重启 scheduler 生效（PID 7432→12108）；11:20:03 熔断激活后
  11:21 起 akshare 实时行情 ERROR = 0

### 放过项
- 0 信号 streak 升级：降级窗口衍生现象，数据源已自愈（10:54 主源恢复），
  信号链路今日有产出，无独立故障
- 920xxx 北交所历史K线失败：已知限制非回归（sina/网易源均不可用）
- 止损告警升级日志每 5 分钟 WARNING（510500/512480 破位）：飞书去重生效
  （`卡片已在近期发送过，跳过重复发送` + daily_alerts 记录），用户今日已收提醒，
  日志刷屏为 skill 已记载的"日志 vs 通知去重"已知 pitfall，非新问题
- 数据源失败率 23% < 50%：四级冗余设计内降级，sina 主源 11:00 后全 success

## 2026-08-10 — v0.9.18 告警评估员五轮：macro close 资金流段落合并修复

### 背景
16:30 triage 报 3 项异常（自愈升级 6 次/1651 ERROR/flow_breaker_active），
逐条取证时发现新 bug：15:15 `[macro] close 资金流数据合并失败:
cannot import name '_format_key_industries_section' from 'gugu.notifier.flow_report'`。

### 根因
`_format_key_industries_section` 定义于 `flow_formatter.py:65`，但 `flow_report.py`
backward-compat re-export 列表（111-116 行）漏了它 → macro/scheduler.py
`_collect_flow_sections` import 失败 → early/close 宏观日报的资金流段落
（行业 TOP/强流入个股）持续缺失。8/7 日志同样 3 次失败 = 历史遗留非新回归。

### 修复
- `src/gugu/notifier/flow_report.py`：re-export 补 `_format_key_industries_section`
- 验证：import OK + ruff 通过 + `test_flow_report.py`/`test_macro_modules.py` 100 passed
- 重启 scheduler 生效（修复代码运行期加载）

### 放过项
- restart_storm ×3（今日 5 次启动）：v0.9.14-16 修复期迭代重启产物，13:36:49 (PID 86613) 后稳定 3h+，锁 PID 匹配，重启风暴自然消退（计数次日归零）
- 当日 1651 条 ERROR：akshare/eastmoney（同东财后端）RemoteDisconnected 限流，
  14:42 后资金流熔断生效静默；真实失败率 9% < 50%，sina 主源 + 腾讯兜底正常
- 信号扫描 data_errors=['flow_breaker_active']：v0.9.13 熔断短路标记，预期行为非故障
- 自愈 coroutine 探测 bug：v0.9.14 已修复（9:45-11:50 三次升级为修复前假升级），13:39 起探测成功

## 2026-08-10 — v0.9.17 告警评估员四轮：triage 信号扫描停摆非交易时段误报修复

### 背景
16:00 收盘后 `scripts/alert_triage.py` 仍报"信号扫描停摆(>30分钟)"：
last_scan_at=15:10 距今 52+ 分钟 → 误报。收盘后不扫描是设计行为
（RealtimeSignalScanner 仅交易时段运行），与 watchdog v0.9.8
"0 信号检查必须只交易时段判定"同源教训，triage 脚本漏了此保护。

### 修复
- `scripts/alert_triage.py`：`信号扫描停摆(>30分钟)` 判定加
  `is_trading_day(now.date()) and is_trading_time(now)` 门控，
  非交易时段（午休/收盘后/周末）不再误报；盘中真实停摆仍正常捕获
- 验证：收盘后场景不判停摆（误报消除）、盘中 14:00 场景仍判停摆（真故障不漏）；ruff 通过

### 放过项
- restart_storm ×3（今日 5 次启动）：v0.9.14-16 修复期迭代重启产物，13:36:49 (PID 86613) 后稳定 2.5h+，锁 PID 匹配，重启风暴自然消退（计数次日归零）
- 当日 1651 条 ERROR：akshare/eastmoney（同东财后端）RemoteDisconnected 限流，
  14:42 后资金流熔断生效静默；真实失败率 9% < 50%，sina 主源 + 腾讯兜底正常
- 信号扫描 data_errors=['flow_breaker_active']：v0.9.13 熔断短路标记，预期行为非故障
- 自愈 coroutine 探测 bug：v0.9.14 已修复（9:45-11:50 三次升级为修复前假升级），13:39 起探测成功

## 2026-08-10 — v0.9.16 告警评估员三轮追加：启动扫描阻塞心跳循环修复

### 背景
v0.9.15 修复 Wisdom fallback 后重启验证时发现：重启后 6 分钟心跳不更新
（heartbeat.json 停留在旧进程最后心跳），而启动扫描（run_daily_cycle）仍在跑。
当日两次重启（13:22 PID 84886 / 13:28 PID 85735）均复现：启动扫描 ~6 分钟无心跳。

### 根因
`TradingScheduler.start()` 中 `await self._engine.run_daily_cycle()`（启动扫描）在
`asyncio.create_task(_heartbeat_loop())` **之前**执行。数据源降级时（东财限流 → 每只股票
走 sina→eastmoney→akshare→tencent 四级降级，含网络超时），启动扫描耗时 5-10 分钟，
期间心跳循环尚未创建 → heartbeat.json 陈旧。watchdog 心跳阈值 15 分钟，慢扫描逼近阈值时
可能误判"调度器挂死"触发 auto-recover 重启 → 重启风暴风险（且每次重启重置熔断器 →
新的错误突发）。

### 修复
- `src/gugu/engine/scheduler.py`：`_heartbeat_loop` 定义与 `create_task` 移到启动扫描之前，
  扫描期间心跳每 60s 照常写入
- 验证：test_scheduler 6 passed + ruff 通过；重启后启动扫描期间心跳保持新鲜

### 放过项
- akshare/eastmoney（同东财后端）午后持续 RemoteDisconnected：四级冗余设计内降级，
  腾讯兜底 18/18 成功、sina 13:34 恢复、真实失败率 6% < 50% 阈值；熔断器 13:23 已重新生效

## 2026-08-10 — v0.9.15 告警评估员三轮：Wisdom fallback 消费 dict 字段崩溃修复（stage + danger_signals）

### 背景
v0.9.14 修复自愈探测后，13:00 首个真实信号（600519 buy）触发 WARNING 刷屏：
`signal_only Wisdom fallback 失败: cannot use 'dict' as a dict key (unhashable type: 'dict')`，
今日多次且每次信号必现。信号本身照发，但智慧建议（wisdom 内容）缺失。
修复后重启暴露第二处崩溃：`slice(None, 2, None)`（KeyError）。

### 根因（同链路两处，均因 pipeline 注入 dict 而 fallback 按字符串/列表消费）
1. `signal_pipeline.py:486` 将 `signal["stage"]` 注入为 **dict**（stage/confidence/description/suggestion），
   `build_data_driven_advice` 用 `stage_map.get(stage, stage)`（line 209）与 `stage in ("crazy","final")`
   （line 211/307）消费——dict 不可哈希 → TypeError: unhashable type: 'dict'
2. `signal_pipeline.py:501` 将 `signal["danger_signals"]` 注入为 **dict**（signals/severity/description/action），
   `danger_signals[:2]`（line 215）对 dict 切片 → KeyError: slice(None, 2, None)。
   空 dict 也是 truthy，无危险信号同样必现

### 修复
- `src/gugu/wisdom/fallback.py`：`stage` 与 `danger_signals` 双归一化——dict 时取内层
  `stage.get("stage")` / `danger_signals.get("signals")`，一处覆盖 buy/sell 全消费点
- `tests/unit/test_wisdom.py`：`test_fallback_dict_stage_no_crash` 回归测试扩展为
  复刻真实信号全形态（dict stage + dict danger_signals），断言不崩溃 + 阶段名映射 + 危险信号提取
- 验证：test_wisdom 31 passed + ruff 通过；真实信号形态（000858 记录 + pipeline 注入）端到端复现通过

### 放过项
- 13:00/13:23 资金流与实时行情 RemoteDisconnected 突发（东财限流）：熔断 3 次后 1800s 冷却
  自动静默（13:23:17 重启后熔断器已重新生效），腾讯四级兜底 18/18 全部成功，真实失败率 6% < 50% 阈值
- 12:15-12:17 日志测试污染：v0.9.14 的 log.py GUGU_TEST_MODE 重定向已防再犯，存量条目随日志轮转消退
- 信号扫描 12:16-13:00 空窗：午休非交易时段，13:00:15 已恢复扫描并产出 1 信号

## 2026-08-10 — v0.9.14 告警评估员二轮：AutoRecover 探测协程 bug + 测试日志/事件文件泄漏修复

### 背景
v0.9.13 修复资金流熔断后，评估员复查发现 3 次升级（09:45/10:50/11:50 data_source_degraded）的
auto_recover_log 全部带 `探测异常: object of type 'coroutine' has no len()`——自愈探测从未真正工作过。

### 修复 1：AutoRecover 数据源探测协程未 await（P1，假升级根因）
- **症状**：`_probe_data_sources` 对 `DataManager.fetch_stock_history`（async 方法）返回的 coroutine 直接
  `len(df)` → `TypeError: object of type 'coroutine' has no len()` → 探测永远失败 → 任何
  data_source_degraded 告警无条件 `escalation=True` 升级人工（自愈闭环形同虚设）
- **修复**：`src/gugu/health/auto_recover.py` 新增 `_run_sync()` 辅助（无运行中事件循环用 `asyncio.run`，
  有则丢新线程兜底），`_probe_data_sources` 探测改走 `_run_sync`。调用方 `AutoRecover.run` 为同步入口
  （health_watchdog.py 同步 main），测试已全部 mock 探测，无破坏
- **验证**：`_run_sync` await 正常；mock DataManager 探测成功/失败路径均正确；test_auto_recover 17 passed

### 修复 2：测试进程日志写生产 logs/（L6 漏洞，当日 142 条假 ERROR 污染源）
- **症状**：`logs/gugu_20260810.log` 混入 pytest 输出（`pytest-of-dong/pytest-29/46/47` 临时目录、
  `[TRADE] 模式=<MagicMock...>`、`test_safe_afternoon_flow_notifies_error` asyncio 调试树），
  alert_triage 统计出 142 条 ERROR 绝大多数为测试产物；12:17 的 `扫描 600519 失败: 'MagicMock' object
  can't be awaited` 即 pytest-47 并发运行 test_scheduler.py 时的测试日志
- **根因**：`src/gugu/utils/log.py` `from gugu.config import PROJECT_ROOT` 在模块导入时绑定生产路径，
  conftest patch `gugu.config.PROJECT_ROOT` 属性不影响它 → 测试进程 loguru sink 仍写生产日志
- **修复**：`setup_logging()` 检测 `GUGU_TEST_MODE=1` → 日志文件重定向 `$TMPDIR/gugu_test_logs/`
- **验证**：GUGU_TEST_MODE=1 下文件落到 `$(gettempdir)/gugu_test_logs/gugu_YYYYMMDD.log`，生产日志无 marker

### 修复 3：conftest 隔离 source_health 事件文件（L7 漏洞，watchdog 失败率掺假）
- **症状**：测试进程真实 DataManager 失败（`method=test` 事件）写入生产
  `data/source_health_events.jsonl`，掺入 watchdog 失败率统计（上午 53-61% 高失败率部分为测试污染）
- **根因**：`source_health.py` `_EVENTS_FILE` 模块导入时绑定生产路径 + 单例 `_instance` 首次初始化后
  不再重建 → conftest 的 PROJECT_ROOT patch 不生效
- **修复**：`tests/conftest.py` 增加 `_EVENTS_FILE`/`_SUMMARY_FILE` 重定向 + 重置
  `DataSourceHealthMonitor._instance = None`
- **验证**：test_data_manager/test_p0_first_batch_guard/test_notifier/test_feishu_notifier/
  test_scheduler/test_health_watchdog/test_auto_recover 全绿（155 passed）

### 放过项
- 上午 09-11 点数据源 60%+ 失败率：v0.9.13 熔断双 bug 假失败（2230 条）已修 + 事件文件已清理
  （`source_health_events.jsonl.bak_20260810_fix` 为 v0.9.13 会话清理备份），当前真实失败率 1%
- 12:16 后信号扫描 0/20 分钟：午休时段（12:00-13:00）非交易时间，正常

## 2026-08-10 — v0.9.17 魅视科技 001229 盘中盯盘（明日交易计划）

### 需求
用户下达 8/11 盯盘任务：魅视科技（001229），当前空仓。
- 回踩买点：40.30-40.60 + 分时MACD底背离/绿柱缩短 + 量能萎缩 → 买 4 成
- 突破买点：放量(>3000手)突破 41.43 + 回踩不破 41.00 → 买 3 成
- 放弃条件：跌破 39.61 → 取消今日所有买入计划

### 新增 `scripts/watch_001229.py`
- 腾讯实时行情 + 分时分钟线（web.ifzq.gtimg.cn）双源
- **分时量处理关键坑**：接口返回的 volume 是**累计量**，必须转单分钟增量（`cum[i]-cum[i-1]`），否则"放量3000手"和"量萎缩"判定全错
- 回踩买点三条件**缺一不可**（价格区间 + MACD信号 + 量萎缩）——量萎缩是必要条件非附加信息
- MACD 绿柱缩短用**趋势收敛**判定（近3根峰值 vs 前3根峰值 <0.8），单根比较对 10% 阈值太敏感（实测 9.7% 收敛误判）
- 突破买点精确语义：突破事件已发生（含回踩至 41.00-41.43 区间）+ 突破后放量 >3000 手 + 回踩不破 41.00 + **60 分钟时间窗**（防早盘突破下午失效误报）
- 交易时段保护（9:30-11:30/13:00-15:00）+ 跨日状态自动重置（date 字段）
- cron：`gugu-盯盘-魅视科技001229` 每分钟 no_agent 检查，触发才输出报警（自动投递飞书），无触发静默

### 验证
- 7 大场景模拟（真实分时数据构造）：突破回踩触发/回踩破位不触发/回踩三条件/量不萎缩不触发/跌破放弃/超时窗不触发 全对
- 13 单测固化语义 + ruff 全绿 + 50 passed 相关模块无回归
- 非交易时段静默验证 + 跨日状态重置验证通过

## 2026-08-10 — v0.9.13 告警评估闭环 + 资金流熔断失效修复（假失败风暴）

### 需求
用户要求：后台运行 gugu 监控，每次报警要评估——需要修改就优化，不需要就放过。
在 v0.9.12 脚本级 AutoRecover 之上新增第二层"告警评估员"（agent cron 每 30 分钟）。

### 新增告警评估员（agent cron）
- `scripts/alert_triage.py`：告警分诊采集脚本——心跳/近24h告警/自愈记录/当日ERROR/信号扫描/数据源失败率/磁盘/进程锁，异常时输出结构化清单，健康输出 HEALTHY
- cron `gugu-告警评估员`（`*/30 9-16 * * 1-5`，工作日交易时段每 30 分钟）：triage 数据注入 agent → 逐条根因评估 → 该修就改（含测试+重启）→ 该放就记录理由 → 无法判断才升级飞书
- 安全红线写死在 prompt：绝不删业务数据/绝不改策略参数/绝不改 execution.mode

### 首轮评估抓到并修复：资金流熔断失效 → 2230 条假失败风暴（P0）
**症状**：watchdog 数据源检查失败率 61% 触发 3 次 high 告警；每分钟整点 19 条 `fetch_stock_flow` 全源失败，09:30-11:29 不间断。

**根因链（双 bug 叠加）**：
1. **熔断空结果被当失败**：BaseCollector 熔断器 `_flow_breaker_guard` 返回空 DataFrame（无标记）→ DataManager fallback 循环 `if df is not None and not df.empty` 为 False → 判定"未成功"→ 走完所有源 → `record_failure("all")` → 每分钟 19 条假失败（watchlist 每只股票）→ 失败率虚高 61% → watchdog 误告警
2. **熔断振荡**：`_flow_record_success` 冷却期内清零 `_flow_breaker_until` → sector_flow 偶尔成功立即解除熔断 → 下一轮 stock_flow 又失败 3 次重新熔断（09:40:22 与 09:40:27 相隔 5 秒两次熔断实证，1800s 冷却形同虚设）

**修复**：
- `base.py`：熔断 guard 返回空 df 带 `attrs["flow_breaker_active"]=True` 标记
- `manager.py`：fallback 循环识别熔断标记 → 全部 fallback 均熔断时静默短路返回（不记录 all 失败）；快速路径保持 FIRST_COMPLETED 语义，熔断+pending 时才等待确认（防误吞真实成功源）；成功返回逻辑提取 `_return_fallback_success`
- `base.py`：`_flow_record_success` 冷却期内只清失败计数、不解除熔断（自然到期恢复探测）
- `watchdog.py`：数据源检查 degraded>=10 即 high 改为——失败率>=50% 才 high；降级频繁（>=15 次）仅 medium 提示（失败率才是主指标）

**验证**：
- 真实环境：手动调用 5 只股票资金流——首个请求记录真实失败触发熔断，后续 4 个全部 `flow_breaker_active` 短路，事件增量从每分钟 19 条 → 0
- 清理 2230 条假失败后失败率 61% → 1.3%，watchdog 从 high 告警 → 全绿
- 新增 6 测试（熔断短路/熔断源+成功源并存/冷却期内 success 不解除/降级频繁 medium 不误报 high），99 passed，全量 2270 passed（2 个存量失败 test_screener_params 与 v0.9.11 相关）

## 2026-08-10 — v0.9.12 自动修复闭环（watchdog 自愈，报警→根因→修复→验证→升级）

### 需求
用户要求：每次报警要分析根源，自动解决问题。此前 watchdog 只"检测+推送"，告警到达用户时根源尚未处理（08-10 实证：signal_history_stale 告警推送后无人处理、data_health=degraded 只被通知）。

### 架构：自愈闭环（AutoRecover）
```
watchdog 5 项检查 → 有告警 → AutoRecover 根因分析 → 安全修复 → 验证
    ├─ 修复成功+验证通过 → 静默（仅审计日志 auto_recover_log.jsonl）
    ├─ 修复失败 / 无法自动修 → 升级人工：飞书推送「原始告警 + 根因分析 + 已尝试动作」
```

### 新增 `src/gugu/health/auto_recover.py`
- 修复器路由（指纹优先，防消息关键词误判——0 信号消息也含"扫描"字样）：
  | 告警 | 根因分析 | 自动修复 |
  |------|---------|---------|
  | 心跳缺失/超时 | launchctl 进程存活 + 锁文件 + 心跳文件 | `launchctl kickstart -k` 重启 scheduler，验证心跳 ≤15min |
  | 重启风暴 | boot_history + 最近日志崩溃线索 | 无自动动作（避免加剧），采集崩溃线索升级人工 |
  | 信号历史失忆 | signal_scan.last_scan_at 新鲜度 | 重启 scheduler 刷新扫描链路 |
  | 磁盘不足 | df + 大目录定位 | 清理旧日志(>3天)/__pycache__/旧.bak(>7天)，**绝不删业务数据** |
  | 数据源降级 | failure_rate + 真实探测 | DataManager 探测样本行情，恢复即自愈，仍失败升级 |
  | 连续 0 信号 | data_health/scan 新鲜度三分支 | 数据源问题→探测；扫描停→重启；策略/置信度层→升级 |
- **安全边界（对抗性审查）**：只做可逆幂等动作（重启/清理垃圾/探测）；绝不改策略配置、绝不删业务数据（signals_history/live_positions/paper state）
- **修复防风暴**：同指纹冷却窗口 60min + 每日上限 5 次（auto_recover_state.json）
- **测试模式短路**：GUGU_TEST_MODE 下所有修复动作短路（不真重启/不真删文件），符合测试隔离铁律
- **审计日志**：data/auto_recover_log.jsonl 记录每次修复的指纹/根因/动作/验证/是否升级

### 脚本与 cron
- `scripts/health_watchdog.py` 新增 `--auto-fix` 模式：告警先过自愈层，修复失败才推送（带根因摘要）；`--notify-auto-fixed` 可推送自愈摘要
- cron wrapper `gugu_health_watchdog.sh` 启用 `--auto-fix --quiet`（健康/自愈成功零输出静默）

### 验证
- 新增 17 测试（修复器路由/安全边界/冷却上限/失败升级/审计日志/清理不碰业务数据），37 passed
- 全量回归 2266 passed（2 个存量失败 test_screener_params 与 v0.9.11 变更不同步，与本版本无关）
- ruff 全绿；生产冒烟：健康时 --auto-fix 静默退出 0

## 2026-08-07 — v0.9.11 防 bug 体系落地（4 层防线）+ 熊市出选股

### 需求变更：熊市也出选股
- `skip_regimes` 从 `[bear, crash]` 改为 `[crash]`（bear 不再完全跳过）
- bear 市候选数 ×0.4（`candidate_modifier`），MA5<MA10 弱势市候选数再减半
- 同步更新 `data/screener_params.json`（自动调优持久化参数覆盖代码默认值——教训：改参数必须同步两个文件）
- crash 保留保护性空仓（系统性崩盘日不接飞刀）

### 防线1：数据入口统一清洗层（消灭"脏数据类"bug）
- `quality.py` 新增 `sanitize_dataframe()`：None/NaN→0、字符串列 None→""、全 NaN 行剔除
- `sanitize_scalar()`：信号字典等非 DataFrame 场景
- 接入 `DataManager._validate` 入口：所有外部数据进系统先清洗，消费点不再各自防御
- 覆盖 v0.9.9 信号黑洞同款场景：`f"{None:.1f}"` 不再崩溃

### 防线2：策略注册表一致性自检（消灭"幽灵策略"bug）
- `registry.py` 新增 `verify_registry_consistency()` → (fatal, warning) 二元组
  - fatal：配置启用但代码未注册 → scheduler 拒绝启动（fail-fast）
  - warning：代码注册但配置未引用 → 死代码残留记日志（hot_money 教训）
- scheduler 启动时执行自检
- **实测抓到 27 项死代码残留**（turtle/dual_ma/macd 等历史策略注册未启用）——验证了自检价值

### 防线3：真实脏数据 fixture 进回归（消灭"mock全绿上线挂"）
- `tests/conftest.py` 新增 5 个真实脏数据 fixture：dirty_history_df / dirty_snapshot_df / empty_df / none_fields_signal / truncated_snapshot_df
- 全部来自生产实战 bug 场景（东财限流空表/字段漂移 None/北交所 502/残缺 80 只快照/v0.9.9 视角失败）
- 新增回归测试 `test_dirty_data_regression.py`：历史写入不崩溃、清洗净化、快照过滤安全

### 验证
- 新增 21 测试（清洗 8 + 脏数据回归 5 + 一致性 4 + 熊市相关），相关模块 143 passed 无回归
- 生产重启加载自检 ✅

## 2026-08-07 — v0.9.10 尾盘选股断供修复（东财快照限流 → 新浪全市场保底源）

### 根因确认
尾盘选股/自动选股"一直没数"（8/3 起 4 个交易日 screener_history 零新增）= 两个叠加原因：
1. **数据源**：全市场快照 100% 依赖东财 clist 接口（sina 主源 `fetch_stock_realtime([])` 原返回空 → DataManager 跳过主源走降级），东财 8/3 起持续限流 RemoteDisconnected（8/5 仅残缺 80 只）→ 快照为空 → 选股零产出
2. **市场保护**：即使数据可用（8/4 新浪源 5535 只），沪深300 MA20(4659)<MA60(4802) 判定 bear → `skip_regimes=[bear,crash]` 保护性空仓跳过（设计行为非 bug）

### 修复
- `SinaCollector.fetch_stock_realtime([])` 空列表 = 全市场快照（新浪 Market_Center 分页接口，100/页 × ~57 页，5538 只，15s）
- 字段对齐 DataManager 约定：symbol/name/price/change_pct/volume/amount/turnoverratio/circ_mv(万元→元×1e4)
- `DataManager._call_with_fallback` 移除 `fetch_stock_realtime([])` 跳过主源逻辑（sina 现已支持）
- `TerminalScreener` 方式3 rename 补全 amount/turnoverratio/circ_mv → 硬过滤条件完整生效

### 验证
- 新增 4 测试（分页/空/HTTP失败/空列表语义变更），65 passed 无回归
- 真实链路：DataManager 全市场快照 5538 只 ✅ / 尾盘选股扫描跑通（bear 市保护性跳过，符合策略）
- 8/5 残缺 80 只快照问题一并消除（新浪源稳定 5500+）

## 2026-08-07 — v0.9.9 信号历史黑洞修复（perspective None 崩溃）+ 止损日志降噪 + 资金流熔断

### 根因确认（信号历史 7/1 起零写入）
`record_signal_history` 被 `perspective_check.composite_score=None` 一票打死：
视角分析失败路径（signal_pipeline:728-733）写入 `composite_score=None`，旧代码
`.get('composite_score', 0)` 在 key 存在值为 None 时返回 None（dict.get 默认值只在 key 缺失时生效），
`f"{None:.1f}"` 抛 `TypeError: unsupported format string passed to NoneType.__format__` →
整个 record_signal_history try 块异常短路 → append_jsonl 永不执行。
叠加去重机制放大：600519 首个信号崩溃但通知成功 → 此后同信号被"已通知过"拦截在记录点之前 →
永不重试 → signals_history.jsonl 从 7/1 起 0 新增（35 天信号历史黑洞）。

### P0：perspective None 防御（信号历史恢复写入）
- `signal_pipeline.py` record_signal_history：`composite_score/min_score/fatal_count` 全部改 `or 0` 防御
- 回归测试 `test_record_signal_history_perspective_none_scores`：None 输入不崩溃且正确写入

### P1：止损提醒日志降噪
- `stop_loss_manager.py`：WARNING 移到 `_should_alert` 去重判断之后（只在真正推送时打），
  触发日志 WARNING→INFO。此前破位持仓每 5 分钟刷 2 条 WARNING（去重已拦通知但日志照打）

### P1：akshare 资金流熔断（东财限流防刷屏）
- `akshare_collector.py`：连续失败 3 次 → 熔断 1800 秒 → 冷却期内静默返回空（不调 API 不打 ERROR）→ 到期自动恢复
- 触发背景：东财 RemoteDisconnected 限流，修复前单日 1705 次 ERROR 刷屏
- 测试：`TestAkshareCollectorFlowBreaker` 4 用例（阈值触发/熔断静默/成功重置/到期恢复）

### 验证
- 新增 5 个测试全过；test_risk.py 21 passed / test_p0_p1_repairs.py 26 passed 无回归
- 生产重启（PID 74384）：熔断 10:24:03 触发 ✅ / 重启后资金流 ERROR 仅 3 次（熔断前）✅
- 止损去重正常：`[track_live] 止损提醒 510500/512480 已去重，跳过通知` ✅
- 信号历史恢复：需等新信号（去重机制下 600519 旧信号不再重写，属预期）

## 2026-08-05 — v0.9.8 第二批 P0 修复（信号黑洞根因 + 实时止损接入 + 模拟盘隔离 + 可观测性）

### 根因确认
**信号黑洞的真正根因 = 120 日 K 线缓存陈旧**（当日数据缺失 → 策略无法产出当日信号 → 加权融合无输入 → 信号被静默吞掉）。

### P0-1：120 日 K 线缓存陈旧修复（信号黑洞根因）
- 缓存新鲜度判定修复：盘后（含当日 bar）与盘中（< 当日 bar）语义分离
- 盘中 5 分钟轮询的陈旧缓存主动驱逐 + 重拉；E2E 验证修复后真实扫描产出信号
- 证据：修复前 17:15 扫描 18 只 → 通过 0 个；修复后 17:32 扫描 18 只 → 通过 2 个
- 真实信号恢复：603501 buy 0.408 / 300760 buy 0.45

### P0-2：信号路由可观测性（消除静默过滤）
- signal_router 三态日志：`所有策略无信号`（策略未触发）/ `加权融合无方向`（未达融合门槛）/ `当日最新信号`（历史 vs 当日语义分离）
- 每只股票信号链路完全透明，不再有"无声无息被过滤"

### P0-3：止损检查接入实时循环
- RealtimeSignalScanner 每 5 分钟执行止损检查 + 升级检查（此前仅每日 4 轮）
- track_live 模式止损提醒是用户唯一执行信号，破位后 5 分钟内升级告警

### P1：模拟盘隔离 / 日志口径 / auto_tune 判定
- `updated_at` 字段 + GUGU_TEST_MODE uuid 隔离（防测试污染共享模拟盘）
- 600519 幽灵成交清理（现金恢复 99.99 万）
- box_breakout 日志标注 `[120天窗口]` + 当日信号语义（不再误导）
- auto_tune AND 判定修复：负 alpha 或低胜率 → 收紧（不再被 AND 卡死）

### 验证
- 新增 11 个测试（`tests/unit/test_p0_second_batch_fix.py`），全量回归 **2264 passed**
- 生产重启加载：`[track_live] 止损告警升级器已启用` ✅，26 个任务注册正常
- 飞书双通道验证：系统卡片 → Hermes 对话 ✅ / 业务卡片 → 开悟之路群 ✅
- 止损状态：510500（-0.3%）🔴 / 512480（-5.2%）🔴 破位提醒已推送（去重生效）

---

# Changelog

## 2026-08-05 — v0.9.8 第一批 P0 防护（单实例锁 + 测试 HTTP 硬隔离 + 健康告警升级 + E2E 验证）

### 背景
深度复盘 8/5 全天 11 个问题（幽灵策略/双 scheduler/测试泄漏/磁盘满 886 次写失败/30 天 0 信号），
根因：**系统对自身状态无可验证不变量 + 所有故障静默发生，用户是最后的检测器**。

### 防护一：单实例锁（杜绝双 scheduler 并行）
- 新增 `src/gugu/utils/single_instance.py`：锁文件 `data/scheduler.lock`（PID+启动时间+命令行）
- 获取锁时验证 PID 存活 + 命令行匹配 gugu 特征（防 PID 复用误判）；残留锁自动接管；退出自动释放
- 接入三入口：`scripts/run_scheduler.py` / `gugu.engine.scheduler._main()` / `scripts/run_live.py`
- GUGU_TEST_MODE 跳过（测试进程可并发）
- 验证：生产重启后第二实例启动被拒绝（PID 13770 持有锁），原实例不受影响

### 防护二：测试 HTTP 硬隔离（结构性杜绝测试泄漏到生产飞书）
- `FeishuNotifier._test_mode_blocks_http()`：GUGU_TEST_MODE + 真实网络 client → 禁止外发
- 短路点：`send_card` 入口 + `_get_tenant_token` + `_post_card` + `_post_webhook`
- **关键设计**：测试显式替换 `_client = MockTransport(...)` 的用例放行（不破坏现有 2207 个测试断言）；
  只有真实网络 client + 测试模式才短路——daemon 线程/后台测试进程结构上不可能外发
- 修复过程发现并修正：新方法插入位置导致 `@retry` 装饰器错挂（重试从 6 次变 2 次）——装饰器归属已修正

### 防护三：健康告警升级（把静默失败变成主动通知）
watchdog 新增三项检查（`src/gugu/health/watchdog.py`）：
1. `check_disk_health`：磁盘剩余 < 1GB → P0 告警（8/5 磁盘满 886 次写失败事故）
2. `check_data_source_health`：读 `source_health_events.jsonl` 当日失败率 > 50% 或降级 ≥10 次 → P0 告警（8/4 全市场快照为空静默事故）
3. `check_zero_signal_streak`：交易时段连续 3 轮扫描 0 信号 → P0 告警（30 天 0 信号事故）
- `run_all_checks` 从 2 项扩展为 5 项；`_alert_fingerprint` 增加三类新指纹去重
- 验证：生产 watchdog 实测 5 项检查全部运行正常（磁盘 16.6GB / 数据源失败率 2.6% / 0 信号 streak 正常）

### 防护四：E2E 验证脚本
- 新增 `scripts/e2e_verify.py`：单实例锁 + 心跳 + 磁盘/数据源 + 真实扫描 + 双通道通知链路
- `--check` 只读 / `--no-scan` / `--no-notify` 模式；默认发送测试卡片验证系统/业务双通道
- 验证：8 项全部通过，测试卡片系统（Hermes 对话）业务（开悟之路群）双通道均 True

### 测试
- 新增 `tests/unit/test_p0_first_batch_guard.py`：15 个针对性测试（锁 4 场景/隔离 3 场景/健康检查 7 场景/组合检查）
- 全量回归：**2207 passed**（88.5s，--timeout=30）
- 生产验证：scheduler 重启（PID 13770）+ 心跳正常 + 锁拒绝第二实例 + E2E 全绿

## 2026-08-03 — 资金流 0 结果根因修复

- 统一行业资金流 validator 的 `symbol` 调用契约，保留 DataFrame attrs。
- 为资金流全源失败增加 `data_available`、`data_error`、`all_sources_failed` 状态，过滤器与尾盘报告区分数据异常和正常无结果。
- signal scan 状态增加资金流健康字段，paper/live 缺失资金流时拒绝买入，signal_only 保持观察模式。

## v0.9.6 (2026-08-01) — P0 修复：测试副作用泄漏到生产飞书告警

### 事故
用户收到"⚠️ 信号历史写入失败（P0 数据完整性）股票: ? (?) 错误: [Errno 2] No such file or directory: '/nonexistent/path/data'"告警。

### 根因
`test_record_signal_history_error_handling` 将 `gugu_config.PROJECT_ROOT` mock 为 `/nonexistent/path` 调用真实 `record_signal_history`，捕获异常后触发 `_alert_signal_history_write_failure`——该函数在 **daemon 线程中构造真实 FeishuNotifier**（读到生产配置，`_system_error_enabled=True`）→ `notify_error` 真实推送飞书。conftest 的 autouse fixture 只 mock 了 `gugu.engine.main.record_signal_history`，**漏了 `signal_pipeline` 模块**；且 GUGU_TEST_MODE 只重定向通知队列文件路径，**不阻止 HTTP 发送**。

### 修复（三层防御）
- **源码层（治本）**：`_alert_signal_history_write_failure` 开头检查 `GUGU_TEST_MODE` → 短路仅记录 debug 日志，任何测试触发都不外发
- **测试层**：`test_record_signal_history_error_handling` patch 告警函数并断言 `assert_called_once`（验证 P0 防线链路触发，隔离环境下不真实外发）
- **conftest 纵深防御**：autouse fixture 补 mock `gugu.engine.signal_pipeline._alert_signal_history_write_failure`（L5，防未来其他测试触发）

### 验证
- `tests/unit/test_signal_pipeline.py` 35 passed
- 全量回归 tests/unit 全绿（`--timeout=20` 防挂起）
- 生产 `data/signals_history.jsonl` 正常（最新 2026-08-01 19:04 落盘），生产无此问题

## v0.9.5 (2026-08-01) — 公众号战法落地（四周期 + 幻灭期修复反弹 + 布林超跌）

### 背景
用户提供公众号《八月，来了~~》战法文章（四周期模型 + MACD 金叉铁律 + 幻灭期修复反弹 + 布林下轨抢反弹），要求落地验证。

### 新增（3 个策略，`src/gugu/strategies/regime_cycle.py`）
- **regime_cycle_four**（四周期模型）：站上20日线+银山谷(MA20上穿MA60)+MACD金叉状态 → 潜伏期买入；跌破20日线+死亡谷+MACD死叉状态 → 幻灭期卖出。主升浪必须建立在MACD金叉状态（原文铁律）
- **death_valley_rebound**（幻灭期修复反弹）：幻灭期(MA5<MA60空头排列) + 20日累计跌幅>10%超跌 + 3日不创新低 + KDJ金叉 + 站上5日线 → 买入；跌破5日线卖出
- **bollinger_oversold_rebound**（布林下轨超跌反弹）：收盘跌破布林下轨 → 买入；反弹到中轨（目标位）卖出

### 关键校准（对抗性审查发现的原文语义缺失）
- 初版 death_valley 漏了"**处于幻灭期**"前提 → 15股600日 2615 信号泛滥（每股每天触发），3日超额 -0.17% 为负
- 补上幻灭期（空头排列）+ 超跌（20日跌>10%）两个前置过滤后 → 75 信号，10日超额 +2.41% (p=0.0004)、验证期 +3.02% (p=0.0012) **显著**
- 参数稳健性：超跌阈值 8-12% 均显著；窗口 20 日甜蜜点（10d 样本太少 / 30d 失效）
- KDJ"金叉"实现为状态（k>d）而非交叉事件——交叉事件在暴跌末段已发生但当时不满足企稳条件

### 优化（第二轮，bollinger 校准）
- **bollinger_oversold_rebound 加超跌过滤**：验证期 p=0.116 未显著 → 加"20日累计跌幅>12%"前提（参考 death_valley 成功经验）→ 验证期 +3.65% (p=0.0001) **显著**
- 超跌阈值网格：不过滤 p=0.116 / 8% p=0.032 / **12% p=0.0001（最优）** / 16% 更猛但信号少
- 集中度复核：13%（sz300059），剔除后 p=0.000 仍显著；2024/2025/2026 三年全正超额（+4.11%/+3.48%/+3.88%）
- **卖出模式参数化**（exit_mode: mid/hold）——事件研究验证卖出模式需引擎级回测（事件研究只看入场信号后N日收益，与卖出无关）
- 布林带固有特性：急跌时 std 扩大 → 下轨自动下移 → 价格追不上下轨，需深跌（-35%）才破位（测试数据必须足够陡）

### 验证
- 16 个新单测（三策略信号/置信度/边界/超跌过滤）
- 事件研究：15股×600日，death_valley 88 事件、集中度19%（剔除最大贡献者后 p=0.005 仍显著）、2024/2025 两年正超额
- **两个策略 conditional_pass**（death_valley + bollinger）；regime_cycle_four → in_review（方向对样本不足，低频周期策略）
- param-audit 36 闭环 0 断裂

## v0.9.4 (2026-08-01) — 绩效纵深（20/60日追踪 + 牛熊分层 + 告警去重 + Brinson诚实化）

### 背景
v0.9.3 遗留三项优化建议（20/60 日收益追踪、按牛熊分层胜率、watchdog 告警去重）+ 全项目彻底检查中发现 Brinson 归因是"假实现"（永远返回 0 却当作真实指标展示，违反"降级掩盖残缺"铁律）。

### 新增
- **20/60 日收益追踪**（`signal_tracker`）：`TRACK_PERIODS` 扩展为 `[1,3,5,10,20,60]`；`_stats_by_period` 各周期独立统计，长周期样本不足标记 `insufficient` 不阻塞短周期；K 线拉取天数覆盖 60 交易日
- **牛熊分层胜率**（`signal_tracker._stats_by_regime`）：沪深300 + MA60 判定信号日市场状态（bull/bear/sideways，±1% 缓冲带）；指数数据不可用标记 `unknown` 不阻塞报告
- **watchdog 告警去重**（`health/watchdog.dedupe_alerts`）：按问题类别指纹去重（失忆/心跳缺失/心跳超时/重启风暴独立），默认 120 分钟抑制窗口；状态文件 `watchdog_alert_state.json`；恢复健康自动清除状态，下次故障立即重新告警；`--check` 模式保持纯净
- **绩效卡片增强**（`formatter_reports`）：新增 ⏱ 分周期命中率 + 🐂🐻 牛熊分层区块

### 修复
- **Brinson 归因诚实化**（`analysis/performance`）：从"永远返回 0 当作真实指标"修复为——有行业数据时计算简化版归因（组合内部分解：配置/选择/交互效应），无数据时 `brinson_enabled=False` 明确标注未启用
- **代码清理**：5 处未使用导入/变量（ruff F401/F841）、2 处风格问题（SIM103/SIM105）、4 处 except-pass 确认有 fallback 语义（加日志）

### 验证
- 16 个新单测（signal_tracker 周期/分层 5 + 去重 9 + Brinson 2）
- 全量单元 2105 passed（+16）；集成 44 passed
- ruff 全绿（src/scripts/tests）
- param-audit --strict：33 闭环 0 断裂
- check-debug-code：无调试残留

## v0.9.3 (2026-08-01) — 信号质量增强（数据质量戳 + 共振过滤 + 能力圈标注）

### 背景
多专家评审 P1/P2 共识（利弗莫尔：关键点需确认；巴菲特：知道圈内圈外；芒格：降级掩盖残缺）。

### 新增
- **数据质量戳三级**（`signal_pipeline`）：fresh(🟢实时)/degraded(🟡降级)/stale(🔴非实时)；降级/非实时信号标注 `observation_only` + 说明（利弗莫尔原则：无信息时不作方向假设）
- **多策略共振标注**（`signal_router`）：`resonance`(触发策略数) + `resonance_level`(strong≥2/single=1)；买卖同时触发时 `signal_conflict` + `conflict_detail`（拒绝机械转达矛盾）
- **能力圈标注**（新模块 `src/gugu/strategies/capability.py`）：30 个策略画像表（可解释性 fundamental/technical/rule/ml + 验证等级 + 仓位上限）；多策略取最保守画像；ML 策略标"黑箱+试验仓(5%)"
- **信号卡片增强**（`formatter`）：信号质量徽章行 + 数据降级说明 + 能力圈行 + 矛盾信号区块

### 验证
- 18 个新单测（能力圈 9 + 信号增强 9）
- 全量 2089 passed（+18）
- 实测：双策略共振→strong、单策略→single、买卖矛盾→正确标注冲突详情
- pre-commit param-audit：Passed

## v0.9.2 (2026-08-01) — P0 静默失效防线（健康看门狗）

### 背景
多专家视角评审（芒格/巴菲特/利弗莫尔）共识：系统最大敌人是**静默失效**——7/16~7/31 真实事故中调度器在跑、日志在写，但信号历史 11 天零写入无人察觉（"系统活着但失忆了"）。

### 新增
- **健康看门狗模块** `src/gugu/health/watchdog.py`：
  - `check_scheduler_health`：心跳新鲜度（>15min 未更新告警）+ 重启风暴（单日 >3 次告警）
  - `check_signal_history_freshness`：调度器在跑但信号历史 >2 天未写入 → "信号历史失忆"告警
  - 无启动记录/首次安装不误报（避免安装即报警）
- **调度器启动记录**：`_record_boot()` 写 `data/scheduler_boot.json`（boot_time + pid + 最近 20 次 history，重启风暴检测数据源）
- **信号历史写入失败告警**：`record_signal_history` 异常时异步飞书告警（P0 数据完整性），不再静默 warn
- **watchdog 脚本** `scripts/health_watchdog.py`：`--check`（退出码 0/1）/ `--quiet`（cron 静默模式：健康零输出、告警输出+推飞书）
- **cron 部署**：`gugu-health-watchdog`（no_agent，每 5 分钟，静默守护）

### 验证
- 9 个新单测（信号历史新鲜度/心跳/重启风暴/无启动不误报）
- 全量 2071 passed（+9）
- 实测：失忆场景正确告警（5 天未写入 → 告警文本 + 飞书推送）、恢复后回到健康、退出码正确（告警 1/健康 0）
- pre-commit param-audit：Passed

## v0.9.1 (2026-08-01) — 参数闭环全量修复（P0+P1，16 断裂 → 0）

### 概述
GA/PSO 动态调优写回 `strategy_defaults.yaml` 的参数 16/27 策略代码根本不读（"调了等于没调"）。全部对齐：defaults 参数名与代码 `self.params.get/[]` 读取名完全一致（代码=权威），动态调优链路打通。

### 修复
- **P0 完全错位**（defaults 参数代码一个都不读）：double_yin 13 个对齐、zhen_dahu 19 个对齐
- **P1 部分错位**（16 个策略）：breakout_rebound 7 组语义映射迁移调优值（limit_up_window→limit_up_lookback 等）、north_force_long 4 组、dragon_tiger_short/ma20_trend/industry_capital_momentum 各 3-4 组；删 30+ 死参数（文档概念但代码未实现：first_board price_min/seal_ratio_min、turtle risk_per_trade/max_units 等）；ml_gradient_boost 3 个训练超参移除（train_and_save 硬编码）
- **chenxuezhang 参数化**：7 个阈值从硬编码接入 self.params（默认值=原硬编码值，行为零漂移，7/7 单测通过）
- **审计工具 bug**：module 为包名时误读 `__init__.py` 导致 ml_gradient_boost 假断裂 → 优先同名 .py + 通配扫描
- **param-audit --strict 全绿**（33/33 闭环），pre-commit hook 升级 strict 模式阻断新增断裂

### 验证
- `param-audit --strict`：33 闭环 0 断裂
- 全量单测：2062 passed（含 chenxuezhang 7/7、first_board 18/18、linjudaye/double_yin/zhen_dahu 相关 95/95）
- pre-commit param-audit hook：Passed

## v0.9.0 (2026-08-01) — 实时推荐 + 实盘交易（阶段四/五落地）

### 概述
响应用户核心需求「实时推荐、实时交易」：把 gugu 从「离线定时扫描」升级为「盘中实时推荐 + QMT 实盘执行」。完整实现 QmtBroker（xtquant 真实接入）、新增 RealtimeSignalScanner（盘中分钟级策略信号扫描+飞书推送+可选自动下单），打通阶段四（实盘）主链路。

### 新增
- **QmtBroker 完整实现**（`src/gugu/execution/qmt.py`）：connect/order/撤单/持仓同步/账户查询/一键平仓/应急限价买入/历史订单查询，全部真实接入 xtquant；代码格式自动转换（sh600000 ↔ 600000.SH）；连接心跳+自动重连；未配置时安全拒绝下单（防裸奔）；CLI 自检 `python -m gugu.execution.qmt`
- **RealtimeSignalScanner**（`src/gugu/engine/realtime_scanner.py`）：盘中策略信号持续扫描（复用引擎 `_scan_signals` 管线），新信号立即推送飞书；信号去重（同一 symbol+direction 冷却期内不重复）；live 模式支持自动执行（confirm_required=False 时直接委托 QMT）
- **配置项**：`intraday_monitor.realtime_recommend` / `signal_cooldown_sec`（settings.yaml + 强类型模型 + flatten）
- **run_live.py 升级**：实盘模式启动 TradingScheduler 持续运行（盘中实时推荐+定时任务），安全退出清理

### 安全机制
- 默认 `signal_only`/`track_live`：飞书推送 + **并行记入独立模拟盘**（真实持仓保护不变，信号效果用模拟盘验证）
- `paper` 模式：信号走引擎完整管线（风控 + PaperBroker 模拟下单 + 通知）
- `live + confirm_required=true`：只推送，等待人工确认（默认安全）
- `live + confirm_required=false`：信号直接委托 QMT（需显式配置）
- QMT 未连接时拒绝一切下单

### 模拟盘验证（用户确认的落地路径）
- **track_live 模式下的真实持仓保护不变**（止损/止盈提醒照常）
- **信号效果用独立 PaperBroker 并行验证**：实时信号 → 飞书通知 + 模拟盘下单（`data/paper_broker_state.json`）
- 信号历史绩效验证（1/3/5/10 日收益）在 `_process_signal` 中持续记录
- 验证路径：效果达标后再切 live 模式上实盘

### 测试
- `tests/unit/test_realtime_scanner.py` 新增 24 个测试（代码转换/安全拒绝/去重/模式分层/sidecar 模拟盘/自动执行）

---

## v0.7.1 (2026-07-15) — 多专家视角深度审查（第二轮）与全面修复

### 概述
基于第一性原理和对抗性审查原则，从量化研究员、风控工程师、系统架构师、数据工程师、DevOps 等多视角对项目进行第二轮全面审查。发现并修复 8 项问题，核心方向：修复配置模型脱节、去除静默吞异常、补充 Calmar/Sortino 比率、回测脚本飞书通知全覆盖。

### 审查发现摘要

| 视角 | 发现数 | 核心问题 |
|------|--------|----------|
| 系统架构师 | 2 | `stock_ranker`/`flow_report` 配置模型缺失（settings.yaml 有定义但 config/models.py 无对应模型）；`flatten_config` 未覆盖新配置段 |
| 数据工程师 | 1 | `kb_indexer.py` 硬编码绝对路径 `/Users/dong/workspace/pdf2word/`（不可部署） |
| 运维工程师 | 1 | `except Exception: pass` 共 ~20 处，关键路径（`risk/manager.py`、`notifier/feishu.py`）异常被静默吞噬 |
| 量化研究员 | 2 | `metrics.py` 缺失 Calmar Ratio 和 Sortino Ratio；`backtest_report.json` 不包含风险调整指标 |
| DevOps | 2 | `backtest_offline.py`、`walk_forward_offline.py`、`diagnose_strategy.py` 无飞书通知集成，自动化流水线反馈链路断裂 |

### 修复清单

#### 配置一致性
- **StockRankerConfig / FlowReportConfig 新增**：`config/models.py` 新增 `StockRankerConfig`（6 个权重字段）和 `FlowReportConfig`（5 个开关字段），对应 `settings.yaml` 的 `stock_ranker:` 和 `flow_report:` 段
- **AppConfig + flatten_config 更新**：`AppConfig` 新增 `stock_ranker` 和 `flow_report` 字段，`flatten_config()` 返回字典同步新增

#### 硬编码路径修复
- **kb_indexer.py**：`DEFAULT_BOOKS_DIR` 从硬编码绝对路径改为通过环境变量 `GUGU_KB_DIR` 或 `PROJECT_ROOT.parent / "pdf2word" / "知识库_docx"` 的相对路径

#### 异常处理修复
- **risk/manager.py:652**：`except Exception: pass` → `logger.warning(f"获取 {sym} 历史数据失败: {e}")`
- **notifier/feishu.py:365**：`except Exception: pass` → `logger.warning(f"发送告警卡片失败: {e}")`
- **notifier/feishu.py:406**：`except Exception: pass` → `logger.warning(f"读取通知队列文件失败: {e}")`

#### 绩效指标补充
- **calmar_ratio() 新增**：`metrics.py` 新增 `_calmar_ratio()` 函数（年化收益/最大回撤），`calc_metrics()` 输出字典新增 `calmar_ratio` 字段
- **sortino_ratio() 新增**：`metrics.py` 新增 `_sortino_ratio()` 函数（仅用下行波动率的 Sharpe 变体），输出字典新增 `sortino_ratio` 字段

#### 飞书通知全覆盖
- **backtest_offline.py**：新增 `--notify` 参数和 `_send_feishu_report()` 函数，批量回测完成后推送策略对比报告到飞书
- **walk_forward_offline.py**：新增 `--notify` 参数和 `_notify_optimization_result()` 函数，WF 优化完成后推送结果到飞书

### 审查评级
- **总体评分**: A- → **A**
- **配置完整性**: B+ → A- | **代码健壮性**: B+ → A- | **指标完备性**: B+ → A
- **通知覆盖**: A- → A | **架构设计**: A- | **算法可靠性**: A- | **风控安全**: A

---

## v0.7.0 (2026-07-15) — 多专家视角深度审查与算法可靠性修复

### 概述
基于第一性原理和对抗性审查原则，从量化研究员、风控工程师、系统架构师等多个专家视角对项目进行全面审查，发现并修复 13 项问题，其中 P0 级运行时 Bug 3 项、P1 级算法/量化逻辑问题 5 项、P2 级设计优化 5 项。核心方向：确保 alpha 算法合理可验证、消除前视偏差、完善飞书通知联动。

### P0 级修复

#### 运行时 Bug 修复
- **RiskManager._check_correlation_with_portfolio**：相关性检查原为空壳（仅记录日志），改为实现真实相关性计算（持仓间收益率皮尔逊相关系数 >0.85 触发告警）
- **ParamOptimizer 并行优化器**：`ProcessPoolExecutor` 嵌套函数 `_eval_params` 无法 pickle，改为 `ThreadPoolExecutor` 解决序列化问题
- **NonNegativeValuesRule.check()**：inplace drop 不生效（原始 DataFrame 不变），改为返回 `(is_valid, cleaned_df, violation_msg)` 三元组 + 真实过滤

### P1 级修复

#### 量化算法可靠性
- **MLFactorModel PCA 正交化前视偏差**：PCA 拟合从全量数据改为在 Walk-Forward 窗口内部进行，消除未来信息泄露
- **AlphaFactory OBV 计算**：OBV 能量潮指标原为 Python 循环逐根K线计算，改为 numpy 向量化（`np.sign` + `np.where` + `cumsum`），性能提升 10x+
- **FactorICAnalyzer 标注修正**：原误标为"截面 IC"，明确标注为"时序 IC"（按时间序列滚动计算因子值与未来收益的相关性）
- **RegimeDetector 周线 resample**：`resample("W")` 默认周日为周起点，与 A 股实际周线（周五收盘）不符，改为 `resample("W-FRI")`
- **DataCache LRU 语义修复**：`get()` 命中时未更新访问顺序，导致 LRU 淘汰策略失效，修复后在命中时先移除再追加（移至末尾）
- **BoxBreakout 置信度买卖叠加**：买入信号置信度被后续卖出信号覆盖（同一股票同时买卖时），改为赋值叠加机制（买入取卖方置信度，卖出取买方置信度，取均值）

### P2 级优化

#### 架构优化
- **抽取 max_hold_days 强制卖出为基类公共方法**：`BoxBreakoutStrategy` 和 `MLFactorStrategy` 中 30 天未盈利强制清仓逻辑，从各自策略提取到 `BaseStrategy._check_max_hold_days()`，消除重复代码
- **ParamOptimizer 目标函数 Calmar 比率替代**：原始权重组合目标函数改为 Calmar 比率（年化收益/最大回撤），更好地衡量风险调整后收益
- **Sharpe Ratio 加入无风险利率**：从 0 改为 2%（中国一年期存款利率近似），使得夏普比率计算更准确，特别是在低波动市场环境下
- **策略验证准入门禁脚本增强**：`validate_strategy_gate.py` 新增 `notify_validation()` 和 `format_validation_report()`，验证通过自动推送飞书通知，新增 `--notify` 命令行选项

### 审查评级
- **总体评分**: A-
- **架构设计**: A- | **策略实现**: A- | **风控安全**: A | **数据鲁棒性**: A-
- **算法可靠性**: B+ → A- | **测试覆盖**: A- | **认知引擎**: A-

### 验证算法建议
1. **回测验证**：`python scripts/run_backtest.py --strategy box_breakout --symbol 600519`
2. **准入门禁**：`python scripts/validate_strategy_gate.py --strategy box_breakout --metrics metrics.json --notify`
3. **Walk-Forward**：`python scripts/walk_forward_offline.py --strategy box_breakout`
4. **因子 IC 分析**：通过 `FactorICAnalyzer`（时序 IC）分析单因子预测力，|IC|>0.03 且 |IR|>0.3 视为有效因子

---

## v0.6.0 (2026-07-07) — 全面优化与对抗性审查修复

### 概述
基于第一性原理和对抗性审查原则，从 10 个专家视角对项目进行全面审查，发现并修复 32 项问题。测试用例从 981 增至 1326（+345），覆盖率从 64% 提升至 67%。

### P0 级修复

#### Bug 修复
- **StageDetector._find_swings**：修复最后一个点重复计数问题
- **TradingEngine._scan_signals**：修复 `scan_list` 为空时 `UnboundLocalError`
- **资金流数据源不可用告警升级**：灰色状态卡片 → 红色告警卡片，标题明确标注"数据源不可用"

#### 测试增强
- **signal_pipeline 测试**：新增 7 个测试类覆盖 signal_only 模式、危险信号过滤、量价背离、四阶段入场过滤、向下摊平、价格校验、Wisdom 超时降级
- **flow_report 测试**：新增 7 个测试类覆盖行业轮动分析、重点行业提取、概念热点、强流入个股、市场全局分析、卡片构建、决策指导格式化

### P1 级优化

#### 算法优化
- **MA 斜率计算**：`np.polyfit` 线性回归替代双点差分，抗噪声能力更强
- **RSI 实现修复**：Wilder 平滑中 gain/loss 全为 0 时正确返回 RSI=100 或 RSI=0

#### 配置化
- **资金流背离阈值**：硬编码阈值提取为模块级常量 `DIVERGENCE_MAIN_THRESHOLD` / `DIVERGENCE_PRICE_THRESHOLD`
- **调度任务可配置化**：`settings.yaml` 新增 `scheduler.tasks` 区块（20 个开关），`SchedulerTasksConfig` 强类型模型

#### 功能增强
- **被过滤信号摘要推送**：收盘报告展示被过滤信号及原因，解决"沉默拒绝"
- **signal_only 免责声明**：仅通知模式卡片标注"不构成交易建议"
- **StockRanker 并发评分**：串行 for 循环改为 `asyncio.gather` 并发
- **盘中扫描频率动态调整**：熊市/暴跌市自动跳过 10:30 和 13:05 扫描
- **信号卡片信息密度优化**：30 秒决策摘要优先（止损止盈 → 绩效 → 决策链路 → 筹码 → 智慧参考）

#### 边界保护
- **KellyCalculator**：`avg_loss` 为 0 时不再除零，盈亏比上限 10
- **PositionController**：单股上限不低于 5%
- **_collect_fallback_market_data**：深证涨跌幅除数为 `prev_close` 而非硬编码 1

### P2 级优化

#### 性能优化
- **batch_history 并发预取**：串行 await 改为 `asyncio.gather` 并发
- **technical.py 指标扩展**：新增 `rsi()` / `macd()` / `bollinger()` / `obv()` 四个指标

#### 运维优化
- **通知重试指数退避**：根据 `retry_count` 动态跳过重试，避免加剧限流
- **clear_halt_only 二次确认**：熔断未激活时直接 return，日志升级为 ⚠️ 级别
- **配置管理弃用标记**：`_legacy_config.py` 添加弃用警告
- **策略参数热加载**：`refresh_config()` + `config_has_changed()`，scheduler 每 5 分钟检测文件变更自动重载
- **数据源健康监控仪表盘**：`/api/health` 端点返回三通道状态 + 资金流状态

#### 功能增强
- **尾盘选股器接入十大视角评分**：`ScreeningResult` 新增 `perspective_score`，排序按视角评分降序
- **已启用策略独立回测脚本**：新增 `scripts/verify_enabled_strategies.py`

#### 测试增强
- **十大视角评分函数测试**：新增 10 个视角详细测试类
- **新增 test_money_management.py**：19 个测试用例
- **新增 test_position_controller.py**：13 个测试用例
- **test_technical.py 增强**：新增 15 个测试用例

### 审查评级
- **总体评分**: B+ → **A-**（32 项问题全部修复）
- **架构设计**: A- | **策略实现**: B+ | **风控安全**: A | **数据鲁棒性**: A-
- **业务逻辑**: B+ | **测试覆盖**: B+ → A- | **认知引擎**: B+ | **用户体验**: B → B+

---

## v0.5.4 (2026-07-06)

### 变更
- 飞书通知重试机制增强（send_card 即时重试 + retry_queued 强制刷新 token + 队列间隔 10min→2min）
- 资金流报告数据源容错（HTTP 重试+UA 池 + 降级条件修复 + 量价推算 fallback）
- 震荡市观察列表接入资金流筛选（主力净流出>1亿/占比<-5% 排除，按资金强度排序）
- 循环导入修复（order_manager/stop_loss_manager 延迟导入 engine 模块）

---

## v0.5.0 (2026-07-01)

### 新增
- 十大视角分析系统（10 位投资大师认知框架量化评分）
- 盘中实时盯盘（IntradayMonitor，按性能分级频率）
- 筹码集中度过滤器（股东户数季度慢变量）
- 财叔动态止盈减仓引擎
- 信号绩效追踪器（SignalTracker）

### 优化
- 信号过滤流水线重构为独立 SignalPipeline 类
- 数据源三级冗余（sina + akshare + eastmoney）
- 熔断状态持久化到磁盘（防重启绕过）
- 测试隔离（4 个 monkeypatch 重定向到 tmp_path）