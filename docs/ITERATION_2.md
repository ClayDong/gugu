# 迭代2 · 多专家评审与修复记录

> 日期：2026-06-22
> 范围：全量源码 + 配置 + 文档
> 方法：7 专家视角 Review + P0/P1/P2/P3 分级

---

## 一、评审统计

- P0: 0 个
- P1: 4 个（全部修复）
- P2: 13 个（记录，本轮不强制修复）
- P3: 5 个（记录，不纳入修复范围）

## 二、P1 问题修复清单

### P1-1: 降级采集器数据未经质量校验

- **文件**: [src/gugu/data/collectors/fallback.py](file:///d:/aispace/gugu/src/gugu/data/collectors/fallback.py)
- **问题**: SinaCollector 和 TencentCollector 的 `fetch_stock_history` 返回前未调用 `validate_stock_history`
- **修复**: 在两个降级采集器的 `return` 前增加 `validate_stock_history(df, code)` 调用
- **验证**: `test_data.py` 全部通过

### P1-2: 日报卡片 dict/list 直接 str() 显示

- **文件**: [src/gugu/notifier/formatter.py](file:///d:/aispace/gugu/src/gugu/notifier/formatter.py)
- **问题**: `format_daily_report` 将 `market_summary`（dict）、`sector_top`（list）、`portfolio_summary`（dict）直接通过 f-string 插入卡片，用户看到的是 Python dict 的 `str()` 表示
- **修复**: 新增 `_format_market_summary`、`_format_sector_top`、`_format_portfolio_summary` 三个内部函数，将 dict/list 转为可读 markdown 文本
- **验证**: `test_notifier.py` 全部通过

### P1-3: 主引擎 TradingEngine 无测试覆盖

- **文件**: [tests/unit/test_engine.py](file:///d:/aispace/gugu/tests/unit/test_engine.py)（新增）
- **问题**: 核心交易流程无测试保障
- **修复**: 新增 16 个测试用例，覆盖：
  - 引擎初始化
  - 非交易日跳过
  - 交易日完整循环
  - 风控拦截信号
  - 风控通过下单
  - 日亏预警/熔断
  - 日报发送
  - 现价更新（正常+异常）
  - shutdown
  - reset_halt
  - 心跳写入
  - 信号扫描（空/有数据/异常）
- **验证**: 16 个测试全部通过

### P1-4: _check_daily_loss 字符串比较枚举（附带修复）

- **文件**: [src/gugu/engine/main.py](file:///d:/aispace/gugu/src/gugu/engine/main.py)
- **问题**: 用 `risk_result.action.value == "warn"` 字符串比较，而非直接比较枚举
- **修复**: 改为 `risk_result.action == RiskAction.WARN`
- **验证**: `test_check_daily_loss_warn/halt` 通过

## 三、附带修复

### test_notifier.py 修复

- `test_format_signal_uses_strategies_fallback`: 修正 elements 索引（formatter 结构变更后）
- `test_feishu_notifier_token_and_send`: 改为实例属性 mock（`_client` 是实例属性）

### test_utils.py 修复

- `test_is_trading_day_exception_fallback`: 改为选择性 mock `__import__`（仅拦截 `chinese_calendar`），避免影响 `date.today()`

## 四、质量验证结果

```
ruff check src tests scripts: 通过
mypy src/gugu: 通过（numpy 类型存根的 Python 3.12 语法警告不影响项目代码）
pytest: 134 passed, 覆盖率 73%（从 70% 提升）
```

## 五、P2/P3 问题清单（仅记录）

### P2（13 个）

1. TencentCollector 委托 SinaCollector，无真正冗余
2. CLAUDE.md 目录结构未更新 → **已修复**
3. 回测引擎未接入完整 RiskManager
4. 缺少 PRODUCT.md 和 TECH.md → **已修复**
5. stock_selector 调用 route 时不传 name
6. PaperBroker direction 未规范化
7. llm_interpret 同步 HTTP 阻塞
8. save_strategy 覆盖同名文件无确认
9. profit_factor 返回 inf
10. 风控告警中英文混用
11. 脚本入口依赖 sys.path
12. 调度器节假日仍触发
13. 历史数据无持久化缓存
14. _holiday_set 只覆盖当年
15. fetch_stock_meta 效率低
16. test_data.py 未覆盖采集器
17. conftest fixture 单一
18. 缺少 fixtures 目录和集成测试

### P3（5 个）

1. 回测 O(n²) 信号生成
2. 日志未输出结构化 JSON
3. DataManager 未校验 freshness
4. test_notifier 未验证日报内容
5. WisdomAdvisor 保留 legacy 路径
