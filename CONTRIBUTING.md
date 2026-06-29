# 贡献指南

感谢你对 gugu 交易系统的关注！

## 行为准则

本项目遵循 [Contributor Covenant](https://www.contributor-covenant.org/) 行为准则。

## 开发流程

1. **Fork 仓库** → 创建功能分支
2. **开发** → 确保通过 lint 和类型检查
3. **测试** → 新增代码要有对应测试，运行 `pytest tests/ -v`
4. **PR** → 清晰描述改动内容和测试结果

## 代码规范

- Python 3.12+
- 使用 `ruff` 进行 lint（line-length=100）
- 使用 `mypy` 进行类型检查（strict 模式）
- 所有新代码需通过 `uv run ruff check src/` 和 `uv run mypy src/gugu/`

## 质量要求

```bash
# 安装依赖
uv sync --group dev

# 运行 lint
uv run ruff check src/gugu scripts

# 类型检查
uv run mypy src/gugu

# 测试（推荐）
uv run pytest tests/ -v --cov=src/gugu --cov-report=term-missing

# 只运行指定测试
uv run pytest tests/unit/test_signal_pipeline.py -v
```

## 项目结构

```
src/gugu/
├── config/        # 强类型配置（Pydantic）
├── data/          # 数据采集与缓存
├── strategies/    # 交易策略
├── backtest/      # 回测引擎
├── risk/          # 三级风控
├── execution/     # 执行层（模拟盘/实盘）
├── engine/        # 主引擎与信号流水线
├── filters/       # 过滤层
├── analysis/      # 高级分析（归因/因子/移动止损）
├── wisdom/        # 交易智慧决策层
├── selector/      # 选股器
├── notifier/      # 飞书通知
├── web/           # FastAPI 仪表盘
└── models/        # 数据模型
```

## 如何贡献

### 添加新策略

1. 在 `src/gugu/strategies/` 下创建新文件
2. 继承 `Strategy` 基类，实现 `generate_signals()`
3. 在 `strategies/registry.py` 注册
4. 在 `config/strategy_defaults.yaml` 添加默认参数
5. 添加单元测试

### 添加新的过滤规则

1. 继承 `ValidationRule` 基类
2. 添加到 `DataValidator` 校验链
3. 在 `SignalPipeline` 的 `process()` 方法中集成

### 修复 Bug

1. 先在 `tests/` 中添加复现该 bug 的测试
2. 修复代码
3. 确认测试通过

## PR 提交 checklist

- [ ] `ruff check` 通过
- [ ] `mypy` 通过
- [ ] 所有测试通过
- [ ] 新功能有对应测试覆盖
- [ ] 文档已更新（如需要）
