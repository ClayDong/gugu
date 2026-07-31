# gugu P0 问题修复方案

> 创建日期：2026-07-28
> 目标：修复所有 P0 问题，达到上线标准

---

## P0-1：策略启用状态不一致

### 问题
- settings.yaml `enabled` 列了 5 个策略，但仅 3 个通过 Walk-验证
- hot_money 和 ma5_momentum 未验证却启用
- 文档/配置/代码三方不一致

### 修复方案

#### 1.1 settings.yaml 改造
```yaml
strategy:
  # 验证通过的策略（无条件启用）
  verified_strategies:
    - box_breakout      # 样本外表现最稳
    - double_yin        # 收益稳定为正
    - rsi_reversal      # 震荡市专用
  
  # 实验性策略（仅 paper 模式启用，signal_only/track_live 仅观察不推送）
  experimental_strategies:
    - hot_money         # 游资策略，逻辑闭环但需 paper 验证
    - ma5_momentum      # 新增待验证
  
  # 向后兼容：原 enabled 列表（当 verified + experimental 都为空时使用）
  enabled: []
```

#### 1.2 策略启用逻辑改造
- `registry.py` 新增 `get_verified_strategies()` 和 `get_experimental_strategies()`
- `engine/main.py` 根据 execution.mode 决定实验性策略是否进入信号融合
- signal_only/track_live 模式下实验性策略信号标记为 `experimental=True`，通知卡片显示"⚠️ 实验性策略，仅供参考"
- paper 模式下实验性策略正常参与

#### 1.3 文档同步
- PRODUCT.md 更新策略启用状态表
- TECH.md 更新配置说明

---

## P0-2：十大视角门禁逻辑矛盾

### 问题
- PRODUCT.md 说"所有买入候选统一经过十大视角门禁"
- TECH.md 说 signal_only 模式下只评分不拦截
- 实际行为与文档描述不符

### 修复方案

#### 2.1 明确行为定义
| 模式 | 十大视角行为 | 说明 |
|------|-------------|------|
| signal_only | 评分展示 + 不拦截 | 所有信号都推送，但卡片显示视角评分 |
| track_live | 评分展示 + 不拦截 | 同上，用户自行判断 |
| paper | 硬过滤（<4分否决） | 模拟资金保护 |
| live | 硬过滤（<4分否决） | 实盘资金保护 |

#### 2.2 代码改造
- `signal_pipeline.py` 中 `process()` 方法明确区分：
  - `execution_mode in (signal_only, track_live)`：视角评分附加到信号，不拦截
  - `execution_mode in (paper, live)`：视角评分 < 4 分直接过滤
- 通知卡片统一显示视角评分和判定结果

#### 2.3 文档同步
- PRODUCT.md 更新"当前运行合约"章节
- TECH.md §3.9 更新集成说明

---

## P0-3：Wisdom 决策层默认关闭

### 问题
- `enable_multi_perspective: false`（默认关闭）
- 28 本书 SKILL 对交易决策零影响
- 大量开发投入无法产出价值

### 修复方案

#### 3.1 决策：默认开启，但控制成本
- 将 `enable_multi_perspective` 改为 `true`
- 限制 `max_perspectives: 2`（每次决策最多 2 个视角，控制 token 消耗）
- 仅在买入决策时启用（止损/止盈/仓位调整用 fallback）

#### 3.2 成本控制
- BookPerspectiveRouter 路由结果缓存（同场景 1 小时内不重新路由）
- LLM 调用超时从 15s 降为 10s
- 仅在 signal.direction == "buy" 时调用 LLM
- 止损/止盈/仓位调整使用 fallback（不消耗 LLM）

#### 3.3 降级策略
- LLM 不可用时自动降级到 fallback
- Circuit Breaker 保持（5 次失败 / 120s 恢复）

---

## P0-4：策略三层无运行时校验

### 问题
- strategy_defaults.yaml（19 个策略参数）
- strategy_manifest.yaml（26 个策略注册）
- settings.yaml（5 个启用策略）
- 三者没有运行时一致性校验

### 修复方案

#### 4.1 启动时自动校验
在 `engine/main.py` 的 `__init__` 中添加 `_validate_strategy_consistency()`：

```python
def _validate_strategy_consistency(self):
    """启动时校验策略三层一致性"""
    # 1. 获取三层数据
    defaults_keys = set(strategy_defaults().keys())
    registry_keys = set(registry.list_strategies())
    settings_keys = set(get_enabled_strategies())
    
    # 2. 校验：settings 中的策略必须在 registry 中
    missing_in_registry = settings_keys - registry_keys
    if missing_in_registry:
        logger.warning(f"策略在 settings 中启用但未注册: {missing_in_registry}")
    
    # 3. 校验：registry 中的策略必须有默认参数
    missing_in_defaults = registry_keys - defaults_keys
    if missing_in_defaults:
        logger.warning(f"策略已注册但无默认参数: {missing_in_defaults}")
    
    # 4. 校验：manifest 状态一致性
    manifest = load_manifest()
    for name in settings_keys:
        m_entry = manifest.get("signal_strategies", {}).get(name)
        if m_entry and not m_entry.get("enabled", False):
            logger.warning(f"策略 {name} settings启用但manifest禁用")
```

#### 4.2 CI 集成
- `.github/workflows/` 添加 `verify_strategies` 步骤
- PR 时自动运行一致性校验

---

## P0-5：WisdomAdvisor 循环依赖防护

### 问题
- SignalPipeline → WisdomAdvisor → Strategy（潜在循环）
- 当前实际无循环（WisdomAdvisor 读 signals_history 而非重新生成信号）
- 但代码结构上没有明确防护

### 修复方案

#### 5.1 添加防护层
在 `wisdom/advisor.py` 中添加 `_in_pipeline` 标志：

```python
class WisdomAdvisor:
    def __init__(self, ...):
        self._in_pipeline = False  # 防止重入
    
    async def advise_async(self, signal, context, _from_pipeline=True):
        if _from_pipeline:
            if self._in_pipeline:
                logger.warning("WisdomAdvisor 重入调用，降级到 fallback")
                return self._fallback_decision(signal)
            self._in_pipeline = True
        try:
            # ... 原有逻辑
        finally:
            if _from_pipeline:
                self._in_pipeline = False
```

#### 5.2 文档标注
- 在 SignalPipeline 和 WisdomAdvisor 的 docstring 中标注调用方向
- 添加架构图说明数据流向

---

## P0-6：SignalPipeline 步骤编号统一

### 问题
- TECH.md 用 0, 0.5, 1, 2, 3, 2.5, 4, 5, 3.5
- PRODUCT.md 用 3.6 表示十大视角
- 两套编号系统并存

### 修复方案

#### 6.1 统一编号系统
采用整数步骤编号：

| 步骤 | 名称 | 说明 |
|------|------|------|
| 0 | 元数据注入 | prev_close, is_st, is_suspended |
| 1 | 四阶段判断 | StageDetector |
| 2 | 危险信号检测 | DangerSignalDetector |
| 3 | 基本面过滤 | FundamentalFilter |
| 4 | 资金流过滤 | MoneyFlowFilter |
| 5 | 行业约束 | IndustryConstraint |
| 6 | 向下摊平检查 | NoAverageDownChecker |
| 7 | 市场状态仓位修正 | RegimeDetector |
| 8 | Wisdom 决策层 | WisdomAdvisor |
| 9 | 四阶段入场过滤 | StageDetector.entry_filter |
| 10 | 十大视角评估 | PerspectiveEngine |

#### 6.2 代码和文档同步更新
- `signal_pipeline.py` docstring 更新
- TECH.md §3.3 更新
- PRODUCT.md §31 轮次更新

---

## P0-7：track_live 止损无强制保护

### 问题
- track_live 模式下止损只推送通知
- 用户未及时查看时亏损扩大

### 修复方案

#### 7.1 分级告警机制
```python
# track_live 止损告警升级策略
# T+0 发现止损触发 → 立即推送飞书
# T+30min 未确认 → 二次推送（标注"紧急"）
# T+60min 未确认 → 三次推送（标注"严重"）
```

#### 7.2 用户配置选项
```yaml
execution:
  track_live:
    stop_loss_alert_escalation: true  # 启用告警升级
    escalation_intervals: [30, 60]    # 升级间隔（分钟）
    max_escalation: 3                 # 最大升级次数
```

#### 7.3 实现位置
- `engine/main.py` 的 `_check_stop_loss()` 中添加告警升级逻辑
- 使用 `data/stop_loss_alerts.json` 跟踪告警状态

---

## P0-8：Web 管理接口未认证

### 问题
- `admin_api_key: ""` + `require_auth_for_sensitive: false`
- 任何人可访问敏感接口

### 修复方案

#### 8.1 强制认证
```yaml
web:
  admin_api_key: "${GUGU_ADMIN_API_KEY}"  # 从环境变量读取
  require_auth_for_sensitive: true       # 强制认证
```

#### 8.2 代码改造
- `web/app.py` 中 `_require_admin_auth()` 在 `require_auth_for_sensitive=true` 时：
  - 检查 `X-API-Key` header
  - 不匹配返回 401
  - 空 key 返回 403

#### 8.3 部署指南
- README.md 添加"生产环境部署"章节
- 说明如何生成和配置 API Key

---

## P0-9：未验证策略上线

### 问题
- hot_money 和 ma5_momentum 未通过 Walk-Forward 验证
- 已在 enabled 列表中

### 修复方案

#### 9.1 建立策略上线 Gate
策略必须满足以下全部条件才能从 `experimental` 升级到 `verified`：

| 条件 | 阈值 | 说明 |
|------|------|------|
| 样本外 Sharpe | > 0 | Walk-Forward 样本外表现 |
| 参数稳定性 | ≥ 50% | 最优参数在折间稳定性 |
| 最大回撤 | < 25% | 风险控制 |
| 平均收益 | ≥ 0 | 不亏钱 |
| 交易次数 | ≥ 30 | 统计显著性 |

#### 9.2 自动化验证
- `scripts/optimize_params.py` 输出验证报告
- 报告包含 4 项门控指标
- 全部通过则自动建议升级到 verified

#### 9.3 当前处理
- hot_money 和 ma5_momentum 移入 `experimental_strategies`
- 在 paper 模式下继续验证
- 验证通过后自动升级

---

## P0-10：hot_money 与信号去重冲突

### 问题
- 信号去重：同交易日 + 同方向 + 同策略 = 只通知一次
- hot_money 是高频策略，可能需要多次入场/离场

### 修复方案

#### 10.1 策略级去重策略
```python
# 不同策略的去重窗口
DEDUP_STRATEGY_CONFIG = {
    "hot_money": {
        "window_hours": 0,  # 不去重（每次信号都通知）
        "min_interval_minutes": 30,  # 但同一方向至少间隔 30 分钟
    },
    "default": {
        "window_hours": 24,  # 默认 24 小时内不重复
        "min_interval_minutes": 0,
    }
}
```

#### 10.2 代码改造
- `engine/main.py` 中 `_is_signal_duplicated()` 根据策略名查配置
- hot_money 策略使用更短的去重窗口

---

## P0-11：基本面过滤器无降级

### 问题
- fundamental.py 依赖 akshare 获取 PE/PB/ROE
- akshare 被限时过滤失败
- 没有 fail-open 设计

### 修复方案

#### 11.1 添加降级逻辑
```python
class FundamentalFilter:
    def check(self, symbol, df):
        try:
            # 尝试获取基本面数据
            fundamentals = self._fetch_fundamentals(symbol)
        except Exception as e:
            logger.warning(f"基本面数据获取失败 {symbol}: {e}")
            # 降级：根据 execution.mode 决定
            if self.execution_mode == "signal_only":
                return True, "数据缺失，signal_only 模式放行"
            else:
                return False, "数据缺失，非 signal_only 模式拒绝"
        
        # 正常校验逻辑...
```

#### 11.2 缓存优化
- 基本面数据缓存 24 小时（PE/PB/ROE 不会每天大变）
- 减少 akshare 调用频率

---

## P0-12：通知队列无告警

### 问题
- notify_queue.jsonl 重试 5 次后丢弃
- 没有告警机制

### 修复方案

#### 12.1 队列监控告警
```python
# feishu.py retry_queued() 中添加
if failed_count > MAX_RETRY_COUNT:
    # 发送"通知失败"告警
    self._send_alert(
        title="⚠️ 通知发送失败",
        content=f"队列中有 {failed_count} 条通知已丢弃，请检查飞书 API"
    )

# 队列长度监控
if queue_size > QUEUE_SIZE_THRESHOLD:
    self._send_alert(
        title="⚠️ 通知队列堆积",
        content=f"队列长度 {queue_size}，超过阈值 {QUEUE_SIZE_THRESHOLD}"
    )
```

#### 12.2 配置
```yaml
notifier:
  queue_size_threshold: 50      # 队列长度告警阈值
  queue_alert_cooldown: 3600   # 告警冷却（秒）
```

---

## P0-13：进程 kill 无自动恢复

### 问题
- watchdog 最多重启 10 次
- 没有 launchd/systemd 级别配置

### 修复方案

#### 13.1 macOS launchd 配置
创建 `com.gugu.trading.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gugu.trading</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/dong/workspace/gugu/.venv/bin/python</string>
        <string>-m</string>
        <string>gugu.engine.scheduler</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/dong/workspace/gugu</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>/Users/dong/workspace/gugu/logs/launchd_out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/dong/workspace/gugu/logs/launchd_err.log</string>
</dict>
</plist>
```

#### 13.2 安装脚本
```bash
# scripts/install_launchd.sh
cp scripts/com.gugu.trading.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gugu.trading.plist
```

---

## P0-14：通知过载

### 问题
- 每日 15+ 条飞书卡片
- 重要信号被淹没

### 修复方案

#### 14.1 通知分级

| 级别 | 类型 | 推送方式 | 示例 |
|------|------|----------|------|
| P0 紧急 | 止损触发、熔断 | 立即推送 + 声音提醒 | 止损触发 |
| P1 重要 | 买入信号、风控告警 | 立即推送 | 买入信号 |
| P2 普通 | 日报、选股、Alpha | 合并推送（盘前/收盘） | 日报 |
| P3 低优 | 宏观、概念、情绪 | 静默推送（不响铃） | 宏观简报 |

#### 14.2 合并推送
- 盘前（09:10）：合并信号汇总 + 选股结果
- 收盘（15:10）：合并收盘信号 + 资金流日报
- 减少推送次数从 15+ → 6-8 次/日

#### 14.3 配置
```yaml
feishu:
  notification_levels:
    urgent: { sound: true, immediate: true }
    important: { sound: false, immediate: true }
    normal: { batch: true, batch_window_minutes: 30 }
    low: { silent: true, batch: true }
```

---

## P0-15：track_live 模式重新定位

### 问题
- track_live 只读持仓 + 止损提醒
- 不如 paper 安全，也不如 live 自动

### 修复方案

#### 15.1 重新定位
将 track_live 明确定位为**"实盘辅助模式"**：

| 功能 | 行为 |
|------|------|
| 读取真实持仓 | ✅ 从 live_positions.json 读取 |
| 止损/止盈提醒 | ✅ 推送通知（带升级机制） |
| 信号生成 | ✅ 生成买卖建议（标记为"建议"） |
| 自动下单 | ❌ 永远不下单 |
| 模拟验证 | ❌ 不模拟 |

#### 15.2 新增"半自动"选项
```yaml
execution:
  track_live:
    auto_stop_loss: false  # 用户预设：是否自动执行止损（需确认）
```

#### 15.3 文档更新
- PRODUCT.md 更新运行模式说明
- 明确 track_live 的适用场景：用户有真实持仓，需要辅助监控但不希望自动交易

---

## 执行计划

### 第一批（配置层修复，低风险）
1. P0-1: settings.yaml 策略分组
2. P0-8: Web API 认证配置
3. P0-13: launchd 配置

### 第二批（核心逻辑修复，中风险）
4. P0-2: 十大视角门禁逻辑
5. P0-4: 策略一致性校验
6. P0-6: 步骤编号统一
7. P0-10: hot_money 去重策略
8. P0-11: 基本面过滤降级

### 第三批（行为变更，高风险）
9. P0-3: Wisdom 决策层启用
10. P0-7: 止损告警升级
11. P0-12: 通知队列告警
12. P0-14: 通知分级推送
13. P0-9: 策略上线 Gate

### 第四阶段
14. P0-5: 循环依赖防护
15. P0-15: track_live 重新定位
16. 文档全面同步
17. 测试验证
17. 最终 Review

---

## 验证标准

- [ ] 所有 P0 问题有明确修复
- [ ] 代码通过 ruff + mypy
- [ ] 测试全部通过
- [ ] 文档与代码一致
- [ ] 无新增 P0 问题
