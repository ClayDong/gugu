# Alpha 生成计划：从规则策略到机器学习多因子模型

## 本机配置约束

- CPU: Intel i7-5650U @ 2.20GHz, 2 核 4 线程
- RAM: 8 GB
- GPU: 无
- Python: 3.14 (uv 环境)

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 机器学习框架 | LightGBM + scikit-learn | 轻量级，CPU 友好，8GB RAM 足够 |
| 深度学习 | 不引入 | 无 GPU，PyTorch 在本机不现实 |
| 因子分析 | 自研 IC/IR 分析 | 轻量，不依赖额外框架 |
| 回测 | 复用现有 BacktestEngine | 已有 A 股规则（T+1/涨跌停/印花税） |

## 目标

1. **因子 IC 分析**：26 个因子哪些有预测力？砍掉无效因子
2. **LightGBM 多因子模型**：用机器学习找因子组合的非线性 alpha
3. **因子正交化**：去除冗余因子，减少过拟合
4. **ML 策略接入**：将 ML 预测分数作为新策略注册，参与信号生成
5. **全量回测验证**：ML 策略必须通过 9 项准入门禁才能启用

## 任务分解

### Task 1: 安装依赖
- pyproject.toml 增加 scikit-learn + lightgbm
- uv sync

### Task 2: 因子 IC 分析模块
- 新建 `src/gugu/analysis/factor_ic.py`
- 计算 26 个因子的 IC（Spearman 秩相关）、IR（IC 均值/IC 标准差）
- 输出因子有效性排名
- 筛选 |IC| > 0.03 且 |IR| > 0.3 的因子

### Task 3: 因子正交化
- 对有效因子做 Gram-Schmidt 正交化或 PCA 降维
- 输出正交化后的因子矩阵
- 消除因子间高相关性

### Task 4: LightGBM 多因子模型
- 新建 `src/gugu/analysis/ml_factor.py`
- 特征：正交化后的有效因子
- 标签：5 日未来收益率（分类：涨/跌/平）
- Walk-Forward 训练：滚动窗口，避免前视偏差
- 输出：每日每只股票的 ML 预测分数（0-1）

### Task 5: ML 策略接入
- 新建 `src/gugu/strategies/ml_factor.py`
- 注册到 `_REGISTRY`：`ml_factor`
- 买入信号：ML 预测分数 > 阈值
- 卖出信号：ML 预测分数 < 阈值
- 走策略准入门禁验证

### Task 6: 全量回测 + 测试
- 新增因子 IC 分析测试
- 新增 LightGBM 模型测试
- 新增 ML 策略测试
- 全量 pytest 验证
- ruff + mypy 验证

### Task 7: 文档更新 + git 提交
- README 更新 ML 因子体系
- strategy_manifest.yaml 增加 ml_factor 策略
- 提交 git

## 约束

- LightGBM 训练数据不超过 500 只股票 × 750 天（8GB RAM 限制）
- 训练使用 CPU，预计单次 < 5 分钟
- 不引入 PyTorch/TensorFlow
- ML 策略必须通过准入门禁才能启用
- 不修改现有策略代码，只新增
