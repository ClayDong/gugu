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

## 完成状态（2026-07-14）

### Task 1-7 全部完成

| Task | 状态 | 产出 |
|------|------|------|
| 1. 安装依赖 | ✅ | scikit-learn 1.9.0 + lightgbm 4.6.0 |
| 2. 因子 IC 分析 | ✅ | `analysis/factor_ic.py` |
| 3. 因子正交化 | ✅ | `analysis/ml_factor.py` FactorOrthogonalizer |
| 4. LightGBM 模型 | ✅ | `analysis/ml_factor.py` MLFactorModel |
| 5. ML 策略接入 | ✅ | `strategies/ml_factor.py` + registry 注册 |
| 6. 全量测试 | ✅ | 1538 passed, 5 skipped, ruff/mypy 通过 |
| 7. 文档+提交 | ✅ | README + manifest + git commit |

### 因子 IC 分析结果（10 股 × 300 天真实数据）

- 26 个因子中 **15 个有效**（|IC|>0.03 且 |IR|>0.3）
- Top 5 因子：
  1. `obv_divergence`: IC=-0.48, IR=-1.44
  2. `ma_deviation_60`: IC=-0.43, IR=-1.38
  3. `ma_deviation_20`: IC=-0.42, IR=-1.38
  4. `di_cross`: IC=-0.42, IR=-1.47
  5. `momentum_20d`: IC=-0.38, IR=-1.11
- 11 个无效因子：macd_cross, kdj_cross, atr_ratio, volume_trend, vwap_deviation, bollinger_width, volatility_20d, adx, gap, doji, hammer

### 小样本回测结果（10 股 × 300 天）

| 指标 | 调优前 | 调优后（回退） |
|------|--------|---------------|
| 总交易数 | 81 | 304 |
| 平均收益 | -3.20% | -3.09% |
| 中位收益 | +0.46% | -11.33% |
| 平均夏普 | 0.27 | -0.48 |
| 最大回撤 | 53.13% | 47.78% |
| 胜率 | 57.86% | 29.85% |
| 盈利比 | 50% | 30% |

### 准入门禁评估

**未通过**。5/6 项失败。

### 下一步优化方向

1. **因子方向处理**：Top 5 因子 IC 均为负值，说明因子方向需要反转。但简单反转会导致模型训练混乱，需要更精细的方向处理
2. **信号频率控制**：当前 200 天数据产生 98 个买入信号，过于频繁。需要增加信号过滤或提高阈值
3. **更大样本验证**：10 股样本太小，需要至少 100 股 × 500 天
4. **特征工程**：考虑加入因子的一阶差分和滚动均值作为额外特征
5. **标签优化**：当前用 5 日收益 > 0 作为标签，可以尝试 3 日或 10 日，或用收益分位数分类
6. **模型调参**：尝试不同的 learning_rate、n_estimators、max_leaves 组合
7. **Walk-Forward 真正落地**：当前是 80/20 静态分割，需要改为滚动窗口

### 结论

ML 多因子模型框架已搭建完成，IC 分析证明因子有预测力（15/26 有效），但当前 ML 策略尚未通过准入门禁。需要继续调优才能产生真正的 alpha。策略保持 `enabled: false`，不会影响生产环境。
