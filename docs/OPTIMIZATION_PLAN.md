# gugu 五项优化执行计划

## T1: 全市场批量回测框架
- 改进 `backtest_all_strategies.py`：增加指标汇总聚合
- 按策略汇总：胜率、平均收益、夏普、最大回撤、盈亏比
- 输出 Markdown 对比报告
- 支持多进程加速

## T2: 宏观数据硬连接 RegimeDetector
- 新增 `MacroRegimeEnhancer` 模块，使用 MacorCollector 数据
- 黄金暴涨+美元下跌=风险偏好→增强多头
- 债市倒挂→增强空头/暴跌判断
- 整合到 MultiPeriodRegimeDetector.detect() 中
- 降级安全：宏观数据不可用时不影响原有价格判断

## T3: TradingEngine 拆分
- 从 main.py (1546行) 中提取：
  - OrderManager：订单创建、执行、状态追踪
  - StopLossManager：止损检查、L3元数据注入
  - SignalExecutionManager：信号处理 + 通知
- TradingEngine 保持为编排层

## T4: 十大视角接入信号流
- 将 BookPerspectiveRouter 接入 SignalPipeline
- 信号通过时集成多视角分析
- 结果注入 signal 字典（mentor_perspectives）

## T5: 端到端集成测试
- 覆盖所有5项优化
- 场景：macor增强的regime检测、拆分后的组件、多视角信号流
