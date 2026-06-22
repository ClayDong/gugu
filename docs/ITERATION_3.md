# 迭代3 · 多专家评审与修复记录

> 日期：2026-06-22
> 范围：复查迭代2修复 + 增量扫描 + 重点纳入 wisdom 架构缺陷
> 方法：7 通用专家 + 3 场景化专家（安全/SRE/策略）+ 决策专家仲裁

---

## 一、评审统计

- P0: 0 个
- P1: 4 个（全部修复）
- P2: 3 个（记录，本轮不强制修复）
- P3: 2 个（记录，不纳入修复范围）
- 不适用专家：算法专家（本项目无核心算法/AI）

## 二、复查结论（迭代2修复）

| 问题 | 状态 | 说明 |
|------|------|------|
| P1-1 降级采集器质量校验 | ✅ 彻底修复 | fallback.py L39/L97 已增加 validate_stock_history |
| P1-2 日报卡片格式化 | ✅ 彻底修复 | formatter.py 三个格式化函数正确工作 |
| P1-3 主引擎测试覆盖 | ✅ 彻底修复 | test_engine.py 16 个测试全部通过 |
| P1-4 枚举比较 | ✅ 彻底修复 | engine/main.py 改为枚举比较 |

## 三、P1 问题修复清单

### P1-1: wisdom skill 依赖 `_refs/` 不提交目录，fresh clone 后功能失效

- **文件**: [src/gugu/wisdom/advisor.py](file:///d:/aispace/gugu/src/gugu/wisdom/advisor.py) + [src/gugu/wisdom/skills/](file:///d:/aispace/gugu/src/gugu/wisdom/skills/)（新增）
- **问题**: legacy 路径指向 `_refs/`（.gitignore 不提交），fresh clone 后 `_skills` 为空
- **修复**:
  1. 将 `_refs/books2skill/library/books/炒股的智慧/skills/` 下 8 个 skill 目录复制到 `src/gugu/wisdom/skills/`
  2. 移除 advisor.py 中的 legacy 硬编码路径（L30-32）
- **验证**: settings.yaml 中 `wisdom.skill_dir: "src/gugu/wisdom/skills"` 指向的目录现在实际存在

### P1-2: 信号通知卡片未展示 wisdom 建议内容

- **文件**: [src/gugu/notifier/formatter.py](file:///d:/aispace/gugu/src/gugu/notifier/formatter.py)
- **问题**: `format_signal` 未读取 `signal["wisdom"]` 字段，用户在飞书卡片中看不到交易智慧建议
- **修复**:
  1. 新增 `_format_wisdom(wisdom)` 函数，将 wisdom dict 转为可读 markdown（含 label_map 中文标签）
  2. 在 `format_signal` 中调用，仅在有内容时追加"交易智慧参考"section
- **验证**: 新增 3 个测试（有wisdom/无wisdom/空wisdom），全部通过

### P1-3: PaperBroker direction 未规范化

- **文件**: [src/gugu/execution/paper.py](file:///d:/aispace/gugu/src/gugu/execution/paper.py)
- **问题**: PaperBroker.order() 未做 `direction.lower().strip()`，与 RiskManager 行为不一致
- **修复**: 在 order() 开头增加 `direction = direction.lower().strip()`
- **验证**: 新增 `test_paper_broker_direction_normalization` 测试，验证 "Buy" 和 " buy " 均能正常买入

### P1-4: L2 熔断可被同日多次调用绕过

- **文件**: [src/gugu/engine/main.py](file:///d:/aispace/gugu/src/gugu/engine/main.py)
- **问题**: `run_daily_cycle` 开头的 `self._risk.reset()` 会清除 L2 熔断状态，同日多次调用可绕过熔断
- **修复**: 在 `run_daily_cycle` 开头增加 `if self._risk.is_halted: return` 检查，熔断状态下直接返回不执行 reset
- **验证**: 新增 `test_run_daily_cycle_halted_skips` 测试，验证熔断状态下不调用 settle_t_plus_1 和 reset

## 四、质量验证结果

```
ruff check src tests scripts: All checks passed!
pytest: 139 passed in 6.55s
覆盖率: 74%（从 73% 提升）
```

## 五、新增测试用例

| 测试文件 | 测试用例 | 覆盖点 |
|----------|----------|--------|
| test_notifier.py | test_format_signal_with_wisdom | wisdom 字段正确展示 |
| test_notifier.py | test_format_signal_without_wisdom | 无 wisdom 时不展示 |
| test_notifier.py | test_format_signal_empty_wisdom | 空 wisdom 时不展示 |
| test_engine.py | test_run_daily_cycle_halted_skips | L2 熔断防绕过 |
| test_engine.py | test_paper_broker_direction_normalization | direction 规范化 |

## 六、P2/P3 问题清单（仅记录）

### P2（3 个）

1. `_load_watchlist` 硬编码 5 只股票，未从配置读取
2. `test_engine.py` 未覆盖 wisdom.advise 被调用的路径
3. LLM API key 通过 httpx 明文传输到自定义 base_url

### P3（2 个）

1. 无进程级监控（内存/CPU/磁盘）
2. 回测 O(n²) 信号生成（延续前轮）
