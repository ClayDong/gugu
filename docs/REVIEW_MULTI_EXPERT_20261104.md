# gugu 多专家独立评审（2026-11-04）

> 评审时点：2026-11-04 22:12 CST
> 评审范围：**当前工作区**（含 37 个 staged 未提交改动），不是 git HEAD（`466d1b5`, 2026-10-08）
> 评审原则：第一性原理 + 对抗性审查；以代码与数据实证为准，文档自述只作「声明」登记
> 六个专家视角独立取证 → 主审交叉复核 → 分层判定

---

## 〇、结论先行

**不给一个是非答案，因为「靠谱」在这个项目里至少有四层含义，而它们的结论相反。**

| 层 | 判定 | 分数 | 一句话依据 |
|---|---|---|---|
| ① 资金安全边界 | ✅ **靠谱** | 8.0 | `verified_strategies: []`、`track_live` 强制拒单、live 需 QMT+人工确认、账户 100 万空仓 |
| ② 研究方法论 / 证伪文化 | ✅ **靠谱** | 8.0 | 三层架构、证伪三件套、ADR 事故驱动、主动证伪断板反包与前视偏差 |
| ③ 策略收益主张 | ❌ **不靠谱** | 2.0 | 零 verified、零样本外正期望、零前向已实现盈亏；README 的 `+2.4%~+3.7% p<0.01` 找不到数据文件 |
| ④ 回测 / 数据可信度 | ❌ **不靠谱** | 2.0 | 行情缓存最多 200 根 bar、`*_500d.parquet` 命中 0、复权口径混用、验证增强零生产调用 |
| ⑤ 工程可持续 / 可复现 | ❌ **不靠谱** | 3.5 | 37 文件未提交、关键脚本被 ignore 却被测试 import、文档四处漂移、覆盖率门禁不生效 |

**核心判断**：作为「**不碰真钱的研究与工程平台**」，gugu 是靠谱的，甚至优于多数个人量化项目；作为「**能产出可交易 alpha 的策略系统 / 可据此下单的信号源**」，它目前不靠谱。

**最危险的不是策略没赚钱，而是文档比代码乐观**。`README.md` 写 36 策略、`+2.4%~+3.7% p<0.01`、2121 测试 / 69% 覆盖；实测是 16 个 signal 策略、`status: enabled` 0 个、行情缓存 0 个 ≥250 行、`signals_history.jsonl` 仅 3 行且 `realized_pnl` 非空 0 条。项目把「**不启用**」做对了，但「**不声称**」没做到。

> 与 2026-10-15 评审（7.5/10）的差异说明：那份评审主要审「架构设计与代码工程质量」，且较大程度采信文档声明；本次按「**结论能否外推到真实决策**」审，并逐条用当前工作区代码/数据复核。两个分数不矛盾，是口径不同。

---

## 一、评审口径与证据等级

**「靠谱」的操作化定义**：① 研究结论可复现；② 数据链路无前视偏差；③ 工程实现有真实测试与门禁；④ 生产准入不放水；⑤ 对外表达不夸大。

| 标记 | 含义 |
|---|---|
| 【A】 | 主审亲自复核（命令输出 / 直接读到的代码行） |
| 【B】 | 专家视角独立取证，且与另一视角交叉印证 |
| 【声明】 | 文档 / README / CHANGELOG 自述，未经实证 |
| 【判断】 | 基于事实的推论 |

复核基准命令（可重跑）：`git status --short`、`git ls-files --error-unmatch scripts/scan_price_volume.py`、`pyarrow` 遍历 `data/parquet_cache/*.parquet`、`grep status: config/strategy_manifest.yaml`、`.venv/bin/python -m pytest tests/ -q --no-cov --basetemp=...`。

---

## 二、六专家评分卡

| 专家视角 | 分数 | 核心结论 |
|---|---|---|
| 策略研究 / 量化 alpha | **3.0** | 生产边界真实，但收益证据几乎不可采信；双状态源分裂 |
| 数据工程 / PIT | **2.0** | 行情缓存陈旧且样本严重不足，回测读取链路命名契约断裂 |
| 回测引擎 / ML 算法 | **2.5** | 费用模型扎实，但成交价假设、停牌 ST、样本隔离三处致命 |
| 架构 / DevOps | **3.5** | 治理写得严谨但多数未生效，干净 clone 不可复现 |
| 风控 / 安全 / 合规 | **5.5** | 资金边界 8/10，声明一致性与合规表达 3/10 |
| 测试 / QA | **4.0** | 2404 收集、核心防线存在自证循环与空断言 |

**双口径综合分**：工程/研究平台口径 **7.0/10**；决策可信度口径 **3.4/10**。
**两个分数并列本身就是结论**：这个项目的价值在「过程资产」，不在「结果数字」。
另：**本次独立全量测试并未通过**（`6 failed / 2255 passed / 143 skipped`，exit=1，详见 §四），这是下调 QA 与 DevOps 分数的主要依据之一。

---

## 三、各视角核心发现

### 3.1 策略研究视角（3.0/10）

- 【A】`config/strategy_manifest.yaml` 16 个 signal 策略状态分布：`conditional_pass` 4（:27/:117/:154/:327）、`in_review` 2（:196/:218）、`pending` 10；**`status: enabled` = 0**。【A】`config/settings.yaml:82` `verified_strategies: []`、`:100` `enabled: []`、`:196` `mode: paper`。
- 【B】真实结果文件全部为负：`data/optimization_report_box_breakout_20261018.json` → `passed:false`、`oos_sharpe=-0.1163`、`total_test_trades=0`；`data/ml_backtest_v42.json` → `gate.passed=False`、`avg_sharpe=-0.7935`；`data/strategy_evaluation_report.json` 8 策略 Sharpe 全负。
- 【B】`data/linjudaye_calibration.json`：train n=2269 `p=3.1e-07` → val n=4294 `p=0.0694`（不显著）→ 2026 n=1230 `p=0.017` **显著为负**。典型的 in-sample 漂亮、样本外翻车。
- 【B】文本与指标打架：`strategy_manifest.yaml:171` 称 linjudaye「正收益可验证，建议保持 enabled」，同条目 `gate_v2.passed: false`、`avg_sharpe=-0.02`；`price_volume_signal:187-188` 宣称「alpha +7.57~13.23% 统计显著」，而 `:180-181` 自认「去掉单股后 p=0.193，证据不足」。
- 【A】`src/gugu/strategies/manifest.py:143`：paper 分支 `return status in {"enabled","conditional_pass"}`，**完全不校验 metrics** 即放行 4 个 gate-v2 未通过策略。
- 【A】`src/gugu/strategies/capability.py` 无 `DISABLED` 枚举，断板反包家族仍是 `IN_REVIEW` + 0.10 仓位（:71/:127/:130）；`src/gugu/strategies/risk_rules.py` 全 `src/` 零 import —— 「认知资产层」目前是**僵尸资产**，且项目自己的 `docs/validation/asset_combo_深度审计报告_20260815.md` 已在 500d 长样本下把其中三条规则证伪。

### 3.2 数据工程视角（2.0/10）

- 【A】行情缓存子集实测（`.venv/bin/python` + pyarrow）：`data/parquet_cache/` **349 个 parquet，行数 min/median/max = 1/90/200，`≥250` 行 0 个、`≥500` 行 0 个、`≥600` 行 0 个**；14 个 `*_500d_none.parquet` 实际每个只有 200 行；日期覆盖 `2025-11-28 ~ 2026-11-04`；347/349 mtime 超过 7 天。
- 【A】命名契约断裂：`data/parquet_cache/*_500d.parquet` 命中 **0**，实际 13 个是 `*_500d_none.parquet`。【B】`scripts/walk_forward_offline.py:94/:130`、`scripts/validate_asset_combo.py:63` 直接 `pd.read_parquet` 并按无后缀名查找 → 零命中、零样本，绕过 `DataManager` 与质量链。
- 【B】复权口径混用：`collectors/fallback.py:82` `adjust_type="none"`（主源 sina），而 akshare/eastmoney/tencent 三个降级源均为 `qfq`；`data/manager.py:214/:447-449` 降级时**用主源的 adjust_type 写入缓存**；`cache.py:167` 盘后 `max_stale=0` → 每交易日全量覆盖，前复权以最新日为基准重算全部历史价 → **Point-in-Time 不成立**。
- 【A】`src/gugu/backtest/validation.py` 的四个 `calc_*` 全项目仅 `tests/unit/test_backtest_validation.py` 引用，生产零调用；**项目自己的** `docs/plans/2026-08-15-code-review-requirements.md:208` 早已记录「四个函数全项目无调用点」 —— 已知未修。
- 【B】前视静态防线纸面化：`tests/unit/test_lookahead_fix.py:35-40` 复刻了一个 `_should_trim` 自测，`:96-98` 仅 `assert hasattr`；CI 无 `shift(-n)` 扫描。清洗链 `quality.py:77` 用 `bfill(limit=3)` 以未来值填历史 bar。
- 【A】`data/` 下 1373 个 `.lock` 文件（全 0 字节，1024 个无对应 parquet），是设计性锁文件泄漏，非死锁，但印证缓存反复「判过期→删除→重写」。

### 3.3 回测 / ML 算法视角（2.5/10）

- 【A】`backtest/engine.py:115` `execution_price_mode="close"` 默认；【B】`:307/:582-589/:864-865` 以**同一根 bar 的当日 close** 成交，全项目零调用方传 `next_open` → 收盘价不可预知却可成交，且「看到当日涨停后才决定当日不买」，系统性偏乐观。事件研究却默认 `next_open`，两套口径不可比。
- 【B】停牌/ST 铁律形同虚设：`engine.py:295-299` 依赖 `is_suspended` 列，而 `data/manager.py:473-476` 的历史数据不带该列 → 恒 False；`is_st` 从不传入，ST ±5% 永不触发。
- 【B】T+1 加仓漏洞：`engine.py:597-602` 加仓不更新 `buy_date` → 加仓当日可清仓；`max_value` 未扣已有持仓，可突破 L1 单股上限。
- 【B】参数优化把「样本外」做成全样本：`scripts/optimize_params.py:867/:885-896` 把全量 `stock_dfs` 喂 GA/PSO，`:913-957` 再用同批数据产出 `test_sharpe/test_return` 喂 GatePolicy，`:976-982` 写回 `strategy_defaults.yaml`；无 holdout、无多重检验校正。WF 版每窗口新建 engine、不传 `initial_state`，跨窗口资金/持仓断裂。
- 【B】ML 两个 P0（10-15 提出）：**标签对齐已修**（`ml_factor.py:246-251` 改为 `join(..., how="inner")` 按索引对齐）；**train-predict skew 未修**（`:418-431` 单行输入仍可用，`:445/:469` `reindex(fill_value=0)` 与训练 `dropna` 口径不一致）；`walk_forward_optimize:323-327` 仍 cherry-pick 最佳窗口模型。
- ✅ 值得肯定：费用模型是全场最扎实的部分（`engine.py:890-905/:923-929`：佣金万2.5 / 最低5元 / 印花税万5 / 沪市过户费 / 滑点0.2% / 北交所 ±30% / 涨跌停方向感知）。

### 3.4 架构 / DevOps 视角（3.5/10）

- 【A】`git diff --cached --shortstat` = **37 文件 +2654/-156，全部 staged 未提交**，`git diff`（unstaged）为空；HEAD 停在 2026-10-08，CHANGELOG 已写到 2026-10-20 v0.9.59。评审结论依赖未提交代码 → 换机器即失效。
- 【A】**干净 clone 必挂**：`.gitignore:71` 忽略 `scripts/scan_*.py`，`scripts/scan_price_volume.py` 未被 git 跟踪（`git ls-files --error-unmatch` 报错），而新增 `tests/unit/test_scan_price_volume_routing.py:20` **import 了它**。`scripts/*.py` 共 101 个，仅 72 个入库。
- 【B】门禁「自证循环」：`manifest.py:134-143` 校验的 `metrics` 来自同一份人手可编辑的 `strategy_manifest.yaml`，其中 `gate_v2` 证据块（`passed`/`evidence_class`/`failed_checks`）**从未被读取**；`tests/unit/test_manifest_runtime_gate.py:43-45` 用 `tmp_path` 手写 `status: enabled` + 10 项 metrics 即通过生产准入 —— 测试把「改配置即放行」当正常行为锁定。`write_defaults(gate_passed=True)` 由调用方硬编码传入。
- 【B】阈值副本与版本残留：`gate_policy.py:77-84` 是 6 项规则，`scripts/walk_forward_offline.py:27-32` 硬编码 5 项；全库 12 处 `gate-v1` 残留，与 `REVIEW_GUGU_20260922.md:30`「无 gate-v1 残留」矛盾。
- 【A/B】文档漂移（无自动断言）：策略数 16（实测）vs 36（README:28、10-15 评审:610）vs 10（STRATEGY_LIFECYCLE.md:14）；测试数 2404（本次 collect）vs 2121+44 / 2277 / 2099 / 2207；版本 0.9.59（pyproject）vs 0.6.0（HEAD）vs v0.9.40（CLAUDE.md:397）；执行模式 `signal_only`（ADR-001）/ `track_live`（CLAUDE.md:57）/ `paper`（实测）三者冲突；Python 3.11 / 3.12 / 3.14 三种声明。
- 【B】CI：双管道标准冲突（uv+3.12 vs pip+3.11）、`bandit || true`、触发分支不含当前 `fix/paper-lifecycle-20260909`、`uv.lock` 陈旧且未用 `--frozen`；无 Dockerfile / CD。

### 3.5 风控 / 安全 / 合规视角（5.5/10）

✅ **资金边界确实成立**（这是全项目最该被肯定的部分）：
- 【A】`track_live.py:205-231` `order()` 无条件返回 `success=False`；`realtime_scanner.py:194-202` signal_only/track_live 只推送不执行。
- 【A】`settings.yaml:202-204` live 需 `broker: qmt` + `confirm_required: true`；`web/app.py:700-705` `/api/order` 有 `mode != "paper"` 硬闸。
- 【A】心跳：`data/heartbeat.json` 最后周期 `2026-11-04T21:51:50`，`status: "shutdown"`，`total_value=1,000,000`、`cash=1,000,000`、`positions_count=0` —— 无真实持仓、无真实资金变动，当前无常驻进程。

❌ **但护栏是可配置的，且声明误导**：
- 【B】`realtime_scanner.py:217` `allow_auto = not cfg.execution.confirm_required` → **改一处配置即开 live 自动委托**；「人工确认」在 live 路径无实现代码；`qmt.py:525-532 emergency_buy_limit` 明示「跳过飞书人工确认」，`emergency_close_all` 绕过风控。`qmt.py:195-297` 是完整真实下单实现，不是占位。
- 【B】10-15 的 P0「Web 下单无二次确认」**只做了 opt-in 开关、默认未生效**：`web/app.py:264-285` `_require_trading_password` 在 `pw_hash` 为空时直接 return，`config/models.py` `trading_password_hash` 默认 `""`，`tests/unit/test_web_api.py:299-300` 明确断言「默认直接放行」。当前 `GUGU_ADMIN_API_KEY` 未注入 → 敏感端点 503 禁用，是有效兜底；但 `scripts/com.gugu.trading.plist:20-21` 有 `CHANGE_ME_TO_SECURE_RANDOM_KEY` 占位弱密钥。
- 【A】`CLAUDE.md:15`「不是自动交易系统：默认 signal_only」与实测 `mode: paper` 不符 —— paper 会**自动模拟下单**。
- 【B】合规表达：主信号卡 `formatter.py` 标题「买入信号」+「建议仓位」，免责声明仅 `:581` 的 signal_only 分支（LLM 解读时消失）；`formatter.py:479` 读 `win_rate` 而 `engine_mixin.py:714-719` 注入的是 `forward_5d_signal_hit_rate` → 恒显示「胜率 0%」；`web/static/index.html` 展示累计盈亏 / 5日收益 / 胜率 / 净值曲线但全文无免责声明；`scripts/scan_price_volume.py` 自称「实盘信号扫描」、直推业务群、无免责（其策略 `in_review`）。
- 【A】`notifier/` 下免责声明仅见于 `perspective_report.py:226/:314`、`formatter_special.py:326`、`formatter.py:581`；主链路缺失。

### 3.6 测试 / QA 视角（4.0/10）

- 【B】专家实测：2404 collected（53s）；15 文件定向子集 `211 passed, 0 failed, 0 skipped`；带 coverage 的全量 3 次 SIGTERM（沙箱资源）。
- 【A】**主审独立全量运行**（`.venv/bin/python -m pytest tests/ -q --no-cov -p no:cacheprovider --timeout=60 --basetemp=/tmp/gugu_review_pytest`）：
  ```
  6 failed, 2255 passed, 143 skipped, 16 warnings in 582.73s (0:09:42)   PYTEST_EXIT=1
  ```
  失败分类（逐条读了 traceback）：
  - `tests/integration/test_optimization_e2e.py` 2 个（`test_order_manager_buy_execution`、`test_order_manager_buy_gate_allows_observation_in_paper`）：`Timeout (>60.0s)`，堆栈落在 `akshare/utils/func.py:49` 的 `time.sleep` 与 `ssl.py:1372` 的 `do_handshake` → **集成测试发起了真实网络请求，离线隔离失效**。这是项目测试设计的真实缺陷，不是环境噪声。
  - `tests/unit/test_engine.py` 3 个（`test_run_daily_cycle_trading_day` / `test_shutdown` / `test_heartbeat_written`）：`FileExistsError ... mkdir '/Users/dong/workspace/gugu/data'`，明确来自宿主 `WorkBuddy .../sitecustomize.py:507` 的目录写入拦截 → 环境限制，非项目代码缺陷；但也暴露测试会触碰生产 `data/` 目录。
  - `tests/unit/test_p0_p1_repairs.py::test_risk_manager_thread_safety`：卡在 `t.join()` 60s 超时，日志反复刷 `L2 HALT` → **疑似 `RiskManager` 状态文件在 5 线程 × 100 次并发下的锁竞争/写放大**，需单独复现确认，不能简单归为环境问题。
  - 【判断】因此「全量测试通过」的文档声明在当前环境**不成立**：2255 passed 但 exit=1；且 2404 收集数与 README 2121+44 / CLAUDE 2277 / 09-22 评审 2207 均不一致。
- 【B】142 个 `test_*.py`（unit 139 + integration 3）、静态 `def test_` 2324、skip 标记 33 处、`xfail` 0。
- 【B】`pyproject.toml:52` addopts 无 `--cov-fail-under`，而 `[tool.quality] min-test-coverage = 70` 只是自定义配置、无人读取 → **70% 门禁代码上从不阻断**；`quality-gate.yml` 有 fail-under 但与主命令不一致。
- 【B】空断言与自证：`test_lookahead_fix.py` 复刻被测逻辑 + `assert hasattr`（生产函数零调用）；`test_web_api.py:786-806` 把 `SignalTracker.generate_report` mock 成固定 dict 后只断言 `success is True`；`test_backtest.py:391/411` 用 `if len(trades)>=2` 包住断言（无交易时 vacuous pass）；`assert True` 2 处。
- ✅ 正向：`test_gate_policy.py`、`test_manifest_runtime_gate.py`、`test_track_live.py`、`test_safety_invariants.py` 是真断言；`test_presentation_contract.py`（10-20 元审查产物）用静态契约锁住了红涨绿跌与编码红线。

---

## 四、交叉复核：哪些指控成立，哪些要打折

| 指控 | 复核结果 |
|---|---|
| 行情缓存最多 200 根 bar、0 个 ≥250 行 | ✅ **成立（限定范围）**：`data/parquet_cache/` 349 个文件确实如此。但全库 506 个 parquet 含另类数据集大文件（最大 147067 行），且有 `2026-11-04` 的新数据 —— 不能说「全库数据都只有 200 行」 |
| `*_500d.parquet` 命中 0、回测脚本零数据 | ✅ **成立**：实测 0 命中、13 个 `*_500d_none.parquet` 各 200 行。影响 `walk_forward_offline.py` / `validate_asset_combo.py` 等**直读 parquet 的离线脚本**；`DataManager` 在线链路不受此影响 |
| 零前向验证样本 | ✅ **成立，且比专家报告更空**：主审实测 `data/signals_history.jsonl` 当前仅 **3 行**、`realized_pnl` 非空 **0**；`paper_execution_ledger.jsonl` **1 行**、PnL 非空 **0**；`cxbr_paper_ledger.jsonl` **不存在**。（专家记录的「59 行 / 56 行 not_submitted」应为轮转前快照，方向一致、数字按当前实测） |
| `validation.py` 四个 `calc_*` 零生产调用 | ✅ **成立**：仅测试与文档引用；且项目自己的 2026-08-15 审查文档已记录未修 |
| paper 模式放行未验证信号 | ✅ **成立**：`order_manager.py:146-159` 注释明写「paper 模式的目的就是成交验证，故放行 observation_only」；`manifest.py:143` conditional_pass 不校验 metrics；`signal_pipeline.py:394-399` 的 observation_only 不覆盖 conditional_pass |
| 门禁可被配置绕过 | ✅ **成立**：`manifest.py` 读的是人填的 metrics，`gate_v2` 证据块不参与决策 |
| `scan_price_volume.py` 被 ignore 却被测试 import | ✅ **成立**：`git check-ignore -v` 命中 `.gitignore:71`，`git ls-files --error-unmatch` 报未跟踪 |
| Web 模块 0% 覆盖 | ⚠️ **需打折**：那是仓库既有 `.coverage` 历史快照；新增（未提交）`test_web_api.py` 已把 `web/app.py` 拉到 68%（定向子集）。真实问题是「**改动未提交 + 无统一可信覆盖报告**」 |
| 全量测试「全绿」 | ❌ **不成立**：主审独立全量跑出 `6 failed, 2255 passed, 143 skipped`，exit=1。其中 3 个 failure 确属宿主 shim 的 `FileExistsError`（可打折），但 **2 个集成测试真实联网超时 + 1 个风控并发卡死**是项目侧问题，不能打折 |
| 覆盖率 14% | ⚠️ **需谨慎**：来自既有 `.coverage` 文件，可能是历史不完整快照，不能直接当作当前真实覆盖率；但「无 fail-under 门禁」成立 |

---

## 五、最致命的五个问题（按 ROI 排序）

1. **门禁自证循环** —— 治理机制验证的是「人填进 YAML 的数字」，不是「产生数字的过程」。`gate_v2` 证据块不被读取，改两个 YAML 即可让任意策略进 `track_live`；paper 模式 `conditional_pass` 完全跳过 metrics 校验。
2. **证据链断裂** —— 零 verified、零样本外正期望、零前向已实现盈亏；README 的显著性数字无数据文件出处；`optimization_report_*` 记录的是失败结果，却被 `format_dashboard()` 按 `enabled: true` 渲染成「启用」。
3. **回测数据基础不成立** —— 349 个行情缓存最多 200 根 bar、500d 文件实际 200 行、离线脚本命名契约命中 0、主源 none 与降级 qfq 混用且缓存无 asof 快照、验证增强四个函数零调用、回测用当日 close 成交。
4. **误导性表达** —— 未验证信号在 paper 模式被模拟下单并包装成「买入信号 + 建议仓位」，主卡片无统一免责；`CLAUDE.md`「不是自动交易系统」与 `mode: paper` 不符；Web 展示收益/胜率无「模拟盘」标注，且胜率字段恒为 0%。
5. **可复现性断裂** —— 37 文件 staged 未提交、HEAD 落后 27 天、关键脚本被 ignore 却被新增测试 import（干净 clone 必挂）、文档/版本/策略数/测试数四处漂移、CI 触发分支不含当前分支；全量测试实测 `6 failed` 且其中 2 个集成测试依赖真实网络、1 个风控并发用例卡死。

---

## 六、正面清单（不能只批）

- **生产边界三层真实有效**：白名单为空 / track_live 拒单 / live 需 QMT+确认，且由测试锁定；账户快照证明从未触达真实资金。
- **主动证伪的工程文化**：断板反包从「9:1 战法」证伪到 `5日超额 -1.5% p=0.04`；前视偏差修正让视觉评分 V1 `+5.95%` → V2 `-0.57%`；10-15 起清理 `rsi_reversal` 与指标矛盾的「Walk-Forward 通过」陈旧文本；gate-v2 增加 `profit_factor ≥1.5` 与分层回撤。
- **ADR + 事故驱动 + 文档密度**：每个架构决策有事故复盘支撑，60+ 份文档，10-20 的「红涨绿跌」元审查还沉淀出 `DOMAIN_CONVENTIONS.md` 单一事实源与 `test_presentation_contract.py`。
- **A 股制度细节与费用模型**：T+1、涨跌停方向感知、停牌/ST 拒单、北交所 ±30%、佣金最低 5 元、印花税、过户费、滑点。
- **诚实的策略生命周期文档**：`STRATEGY_LIFECYCLE.md:20` 明写「这是诚实的状态，不是系统失灵。系统在不知道策略是否赚钱时，不会假装知道」—— 这个判断本身完全正确，问题出在其他文档没跟上它。

---

## 七、第一性原理：为什么会这样

根因不是「代码写得差」，而是**三条链各自闭环、互不咬合**：

```
治理链  manifest / gate-v2   → 验证「人填的数字」，不验证产生数字的过程  ⇒ 自证
证据链  回测 / 事件研究      → 产出一次性 JSON + 文本，无 hash / asof / 版本绑定 ⇒ 不可复核
呈现链  卡片 / 日报 / README → 读「声明」而非「证据」                     ⇒ 乐观漂移
```

一个具体表现：`format_dashboard()`（`manifest.py:246-295`）只看 `enabled` 字段；16 个策略顶层 `enabled: true` 全在，于是看板把 16 个未验证策略全渲染成启用 —— 这正是「36 个策略」错觉的来源之一。**系统在执行层是保守的，在叙述层是激进的。**

---

## 八、整改优先级（每条都带可验证的验收方式）

| # | 动作 | 验收 |
|---|---|---|
| P0-1 | 先把 37 个 staged 改动提交并打 tag；修 `.gitignore:71` 或 `git add -f scripts/scan_price_volume.py`；CI 合并为单一管道（固定 Python、`uv sync --frozen`、移除 `bandit \|\| true`） | 在**干净 clone** 跑 `pytest tests/unit/test_scan_price_volume_routing.py` 通过；`git ls-files` 覆盖 pytest 收集到的全部模块 |
| P0-2 | 门禁去自证：由 `validate_strategy_gate.py` 生成带 hash + 生成时间的独立 evidence 文件；`is_runtime_enabled` 改为读取并校验 `gate_v2.passed` / `evidence_class`；`write_defaults` 去掉 `gate_passed=True` 硬编码 | 新增**负向测试**：篡改 `settings.yaml` / `strategy_manifest.yaml` 后生产准入必须仍为空；CI 断言 manifest metrics 与 evidence hash 一致 |
| P0-3 | 回测数据门面统一：离线脚本禁止直读 parquet，全部走 `DataManager` 或带 `validate_stock_history(df, now=asof)` 的门面；缓存文件名加 `asof_date`、禁止覆盖、写入前断言 `len(df) >= days`；主源/降级 adjust_type 分离 | `pytest` 断言 `*_500d_*` 每个 ≥500 行；同一 symbol 在 T 与 T+1 读 T-60~T-1 的 close 逐行相等（前复权回填检测）；pre-commit 禁止 `scripts/` 出现 `read_parquet` |
| P0-4 | 呈现层统一：`disclaimer()` 强制注入所有信号卡/日报/Web 页脚/scan 推送（「实验性 · 模拟盘 · 不构成投资建议」）；`conditional_pass` 买入信号强制 `observation_only=True`；修 `formatter.py:479` 字段名，样本 <30 不展示胜率；Web 收益区加「模拟盘」标注 | 全库模板 grep + 一条 E2E 断言；故意删免责声明后测试变红 |
| P0-5 | 证据账本：建立 30+ 跨日期、跨市场状态的**前向已实现盈亏**记录（`realized_pnl` 非空），策略级/日期级报告；`validation.py` 四个 `calc_*` 接入准入链路；多重检验校正（Bonferroni / Deflated Sharpe） | `wc -l data/paper_execution_ledger.jsonl >= 30` 且 `realized_pnl` 100% 非空，作为任何策略声称「通过」的硬门槛 |
| P1 | 修 `test_lookahead_fix.py`（删复刻与 `assert hasattr`，直调 `_scan_signals`）、删 2 处 `assert True`、addopts 加 `--cov-fail-under=70` 并设分模块阈值；`trading_password_hash` 改 fail-closed；把实盘硬闸下沉到 `QmtBroker.order()` | 故意把 `_scan_signals` 的 `>= "15:00"` 改成 `>= "16:00"` 后测试必须变红；`confirm_required=false` 时 `QmtBroker.order` 仍拒绝 |

---

## 九、PM 七问式收尾

| 问题 | 回答 |
|---|---|
| 什么被「错误定价」了？ | 项目的**自我认知**：文档把自己定价成「已通过验证的多策略系统」，实际是「零验证策略的研究平台」 |
| 当前「价格」反映了什么？ | README 的 36 策略 / `+3% p<0.01` / 69% 覆盖，与 10-15 评审的 7.5/10 |
| 什么能证明论点？ | 任选 1 个策略跑通 gate-v2 + 30 条前向已实现盈亏 + 干净 clone 全绿 |
| 什么能推翻论点？ | 若出现带 hash 的 evidence 文件、可复现的样本外正超额、PIT 快照，本报告负面结论即被推翻 |
| 为什么是现在？ | 37 个未提交改动、11-01 起启发式优化才真正生效、行情缓存 347/349 已陈旧、系统当前 `status: shutdown` |
| 什么会改变评级？ | 从「等待证据」→「持有」的唯一条件是 `verified_strategies` 非空且证据可独立复核 |
| 还缺什么证据？ | 独立 holdout、多重检验校正、PIT 快照、clean-clone CI、结构化前向盈亏账本 |

**行动分类：`等待证据` + `重新评估`。**
**评级**：对「工程/研究资产」→ **Hold（正面）**；对「策略收益主张」→ **放弃（不据此下单）**；对「生产自动交易」→ **维持现状，不放宽门禁**。

---

## 附录：关键证据索引

| 结论 | 位置 |
|---|---|
| 生产白名单为空 / paper 模式 | `config/settings.yaml:82,100,196` |
| 16 策略状态、0 个 enabled | `config/strategy_manifest.yaml:27,66,117,154,196,218,238,258,276,293,327,347,365,380,395,410` |
| paper 放行 conditional_pass | `src/gugu/strategies/manifest.py:143` |
| paper 放行 observation_only | `src/gugu/execution/order_manager.py:146-159` |
| observation_only 不覆盖 conditional | `src/gugu/engine/signal_pipeline.py:394-399` |
| track_live 拒单 | `src/gugu/execution/track_live.py:205-231` |
| live 单点开关 | `src/gugu/engine/realtime_scanner.py:217` |
| 回测默认当日 close 成交 | `src/gugu/backtest/engine.py:115,577,635` |
| 验证增强零调用 | `src/gugu/backtest/validation.py:68,89,164,221` + `docs/plans/2026-08-15-code-review-requirements.md:208` |
| 免责声明缺口 | `src/gugu/notifier/formatter.py:438,479,581` |
| 行情缓存样本量 | `data/parquet_cache/*.parquet`（pyarrow 实测） |
| 零前向盈亏 | `data/signals_history.jsonl`(3 行) / `data/paper_execution_ledger.jsonl`(1 行) |
| 可复现性断裂 | `.gitignore:71` + `tests/unit/test_scan_price_volume_routing.py:20` |
| 运行态 | `data/heartbeat.json`（2026-11-04T21:51:50, shutdown, 100 万空仓） |

【MISSING】本次未验证：launchd 是否已加载 plist 注入 `GUGU_ADMIN_API_KEY`、xtquant/QMT 是否真实安装登录、600 日事件研究的原始输出、`git tag` 列表、pre-commit 是否实际安装生效、ML 模型生产调用方。

---

*本报告仅供研究参考，不构成个人投资建议。*
