"""信号路由：多策略信号融合。

融合规则：
- unanimous: 全票通过
- majority: 多数通过
- any: 任一策略触发
- weighted: 加权融合（支持市场状态自适应权重）
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.analysis.regime_strategy_adjuster import compute_regime_weights
from gugu.analysis.strategy_pool import StrategyPool
from gugu.config import get_config
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class SignalRouter:
    """多策略信号融合器。"""

    def __init__(
        self,
        strategies: list[Strategy] | None = None,
        fusion_rule: str | None = None,
        min_confidence: float | None = None,
        *,
        strategy_pool: StrategyPool | None = None,
    ) -> None:
        """初始化信号路由器。

        Args:
            strategies: 策略列表，默认从配置加载。
            fusion_rule: 融合规则（any/majority/unanimous），默认从配置读取。
            min_confidence: 最低置信度阈值，默认从配置读取。
            strategy_pool: 策略池（用于加权融合 + 动态调整），
                默认创建新实例。传入共享实例可实现策略表现跟踪。
                显式传入参数时优先于配置，便于测试注入（T-02 修复）。
        """
        cfg = get_config().strategy
        self._fusion_rule = fusion_rule or cfg.signal_fusion
        self._min_confidence = (
            float(min_confidence) if min_confidence is not None else cfg.min_confidence
        )
        self._strategies = strategies or []
        # P2 修复：集成 StrategyPool 实现加权融合（代替简单的 majority/any/unanimous）
        self._pool: StrategyPool | None = strategy_pool
        # 当 fusion_rule 显式传入 'weighted' 且提供了 pool 时启用加权融合。
        # 默认池按 execution.mode 隔离 verified/experimental 权重作用域。
        if self._pool is None and self._fusion_rule == "weighted":
            execution_mode = getattr(getattr(get_config(), "execution", None), "mode", None)
            self._pool = StrategyPool(execution_mode=execution_mode)

    def add_strategy(self, strategy: Strategy) -> None:
        """添加策略。"""
        self._strategies.append(strategy)

    def route(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str = "",
        regime: str | None = None,
    ) -> dict[str, Any] | None:
        """对单只股票融合多策略信号。

        Args:
            df: 行情数据
            symbol: 股票代码
            name: 股票名称（可选，用于飞书卡片展示）
            regime: 市场状态（bull/bear/sideways/crash/rally），
                   提供时启用市场状态自适应权重调整

        Returns:
            融合后的信号 dict，无信号返回 None
            {
                "symbol": str,
                "name": str,
                "direction": "buy"/"sell"/"hold",
                "confidence": float,
                "strategy": str,           # 触发策略字符串（逗号分隔）
                "strategies": [触发策略名],
                "reason": str,
                "regime": str,             # 市场状态（如有）
            }
        """
        if df.empty or len(df) < 30:
            logger.debug("{} route 跳过: 数据不足 {} 行", symbol, len(df))
            return None

        votes: list[tuple[str, int, float]] = []  # (strategy_name, signal, confidence)

        for strategy in self._strategies:
            try:
                result = strategy.generate_signals(df)
                if result.empty:
                    continue
                last = result.iloc[-1]
                sig = int(last.get("signal", 0))
                conf = float(last.get("confidence", 0))
                if sig != 0:
                    votes.append((strategy.name, sig, conf))
                else:
                    # P0-2 可观测性（2026-08-05）：策略当日无信号也要留痕——
                    # 此前"通过 0 过滤 0"无法区分「策略没触发」vs「触发被过滤」
                    logger.debug(
                        "{} 策略 {} 当日无信号（signal=0）最后bar={} conf={:.3f}",
                        symbol,
                        strategy.name,
                        df["date"].iloc[-1] if "date" in df.columns else "?",
                        conf,
                    )
            except Exception as e:
                logger.warning("策略 {} 在 {} 执行失败: {}", strategy.name, symbol, e)

        if not votes:
            # P0-2 可观测性：全策略无信号（区别于"有信号但被过滤"）
            logger.info(
                "{} 所有策略无信号（{} 只策略，最后bar={}）——非过滤，是策略未触发",
                symbol,
                len(self._strategies),
                df["date"].iloc[-1] if "date" in df.columns else "?",
            )
            return None

        # 统计买卖票数（仅统计实际触发投票的策略）
        buy_votes = [(n, c) for n, s, c in votes if s == 1]
        sell_votes = [(n, c) for n, s, c in votes if s == -1]
        total_votes = len(buy_votes) + len(sell_votes)

        # P0 修复：卖出硬否决（趋势破坏时不入场 — 《炒股的智慧》铁律）
        # 趋势类/突破类策略若发出 sell，说明趋势已破坏，禁止买入
        # 这些策略的 sell 信号覆盖任何 buy 信号
        # v0.7.3 优化：策略池精简后，仅保留核心趋势/突破类策略的卖出硬否决
        # v0.9.7 P0 修复：移除 hot_money（2026-08-01 样本外证伪已从注册表删除）
        _trend_breakdown_strategies = {
            "box_breakout",
        }
        sell_strategy_names = {n for n, _ in sell_votes}
        trend_breakdown = sell_strategy_names & _trend_breakdown_strategies

        direction = "hold"
        triggered = []

        # P2 修复：跨类别投票平衡 — weighted 模式下先按类别分组，再跨类别融合
        # 防止同类策略（如3个突破类）过投票压制其他类别（如1个主力行为类）
        # 每个类别的权重上限 = 1 / 有效类别数
        avg_conf = 0.0
        if self._fusion_rule == "weighted" and self._pool is not None:
            # 按类别分组统计 buy/sell 得分
            category_buy: dict[str, float] = {}
            category_sell: dict[str, float] = {}
            category_voters: dict[str, int] = {}
            strategy_to_category: dict[str, str] = {}

            for strategy in self._strategies:
                cat = getattr(strategy, "category", "unknown")
                strategy_to_category[strategy.name] = cat
                if cat not in category_voters:
                    category_voters[cat] = 0
                category_voters[cat] += 1

            # 收集按类别的 buy/sell 得分
            for n, s, c in votes:
                cat = strategy_to_category.get(n, "unknown")
                if s == 1:
                    category_buy[cat] = category_buy.get(cat, 0.0) + c
                elif s == -1:
                    category_sell[cat] = category_sell.get(cat, 0.0) + c

            # 跨类别融合：每个类别内先归一化（除以投票策略数），
            # 再跨类别求平均（每个类别权重 = 1/有效类别数）
            active_categories = set(category_buy.keys()) | set(category_sell.keys())
            n_cats = max(len(active_categories), 1)

            # 将 votes 转为 StrategyPool 可接受的信号格式，
            # 但用 category 分组的权重覆盖原始权重
            pool_signals = [
                {"direction": "buy" if s == 1 else "sell", "confidence": c, "strategy_name": n}
                for n, s, c in votes
            ]

            # 计算跨类别平衡权重：每个策略权重 = (1/类别数) / 该类策略数
            balanced_weights: dict[str, float] = {}
            for n, _, _ in votes:
                cat = strategy_to_category.get(n, "unknown")
                n_in_cat = category_voters.get(cat, 1)
                balanced_weights[n] = 1.0 / (n_cats * n_in_cat)

            # 市场状态自适应权重（在 balanced_weights 基础上调整）
            effective_weights = dict(balanced_weights)
            if regime:
                # 只对本路由器实际加载的策略做 regime 调整；即使外部注入了
                # 含实验策略的旧 StrategyPool，也不能让它稀释生产策略权重。
                loaded_names = {strategy.name for strategy in self._strategies}
                base_weights = {
                    name: weight
                    for name, weight in self._pool.get_weights().items()
                    if name in loaded_names
                }
                raw_adjusted = compute_regime_weights(base_weights, regime)
                total_raw = sum(raw_adjusted.get(n, w) for n, w in base_weights.items())
                if total_raw > 0:
                    regime_weights = {
                        n: raw_adjusted.get(n, w) / total_raw for n, w in base_weights.items()
                    }
                    # 混合：70% 跨类别平衡权重 + 30% regime 权重
                    for n in balanced_weights:
                        if n in regime_weights:
                            effective_weights[n] = (
                                0.7 * balanced_weights[n] + 0.3 * regime_weights[n]
                            )

            pool_result = self._pool.weighted_fusion(
                pool_signals, override_weights=effective_weights
            )

            if pool_result["direction"] == "buy":
                direction = "buy"
                triggered = pool_result.get("strategies", ["weighted"])
                avg_conf = pool_result["confidence"]
            elif pool_result["direction"] == "sell":
                direction = "sell"
                triggered = pool_result.get("strategies", ["weighted"])
                avg_conf = pool_result["confidence"]
            else:
                # P0-2 可观测性：加权融合无方向（票数/置信度未达内部门槛）
                logger.info(
                    "{} 加权融合无方向: votes={} buy={} sell={} pool_conf={:.3f}——"
                    "有策略触发但未达融合门槛（非策略未触发）",
                    symbol,
                    len(votes),
                    len(buy_votes),
                    len(sell_votes),
                    float(pool_result.get("confidence", 0) or 0),
                )
                return None

        elif self._fusion_rule == "unanimous":
            # 所有投票策略方向一致
            if total_votes > 0 and len(buy_votes) == total_votes:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif total_votes > 0 and len(sell_votes) == total_votes:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]
        elif self._fusion_rule == "majority":
            if len(buy_votes) > len(sell_votes) and len(buy_votes) > total_votes / 2:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif len(sell_votes) > len(buy_votes) and len(sell_votes) > total_votes / 2:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]
        else:  # any — 买卖同时触发时，比较总置信度，高者胜；平盘sell优先
            if buy_votes and sell_votes:
                buy_total_conf = sum(c for _, c in buy_votes)
                sell_total_conf = sum(c for _, c in sell_votes)
                if sell_total_conf >= buy_total_conf:
                    direction = "sell"
                    triggered = [n for n, _ in sell_votes]
                else:
                    direction = "buy"
                    triggered = [n for n, _ in buy_votes]
            elif buy_votes:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif sell_votes:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]

        if direction == "hold":
            return None

        # P0 修复：卖出硬否决 — 趋势破坏时强制 sell，覆盖任何 buy
        # 即使加权融合结果是 buy，只要趋势类策略发出 sell，就强制改为 sell
        if direction == "buy" and trend_breakdown:
            logger.warning(
                f"{symbol} 趋势类策略 {trend_breakdown} 发出卖出信号，"
                f"强制覆盖 buy → sell（趋势破坏时不入场）"
            )
            direction = "sell"
            triggered = list(trend_breakdown)
            trend_sell_conf = [c for n, c in sell_votes if n in trend_breakdown]
            avg_conf = (
                max(sum(trend_sell_conf) / len(trend_sell_conf), 0.8) if trend_sell_conf else 0.8
            )

        # P0 修复：多策略共识折扣
        # 原逻辑：单策略触发时 avg_conf = 该策略 confidence（常为 1.0），无共识惩罚
        # 修复：单策略信号置信度打折，多策略共识才给高置信度
        # A股高噪声环境，单策略信号可靠性远低于多策略共识
        # v0.9.7 P0 修复：共识折扣从 0.7+0.3*ratio 放宽为 0.85+0.15*ratio——
        # 原折扣过重（单策略直接打 7 折），叠加成交量折扣后置信度系统性低于 0.4
        # 门槛导致 30 天 0 信号。单策略 0.85 起步、多策略接近 1.0，仍保留共识溢价。
        # 非加权模式：计算获胜方向的平均置信度
        if self._fusion_rule != "weighted":
            chosen_votes = buy_votes if direction == "buy" else sell_votes
            avg_conf = sum(c for _, c in chosen_votes) / len(chosen_votes) if chosen_votes else 0
        # P0 修复：共识折扣分母使用实际投票数（非总策略数）
        # 很多策略在当前市场/个股上不产生信号（sig=0），不算投票
        # 例：10个策略中只有2个策略有效且一致买入 → 共识度2/2=100%，而非2/10=20%
        active_voters = max(len(votes), 1)
        agreeing_count = len(triggered)
        if active_voters > 1:
            consensus_ratio = min(agreeing_count / active_voters, 1.0)
            consensus_factor = 0.85 + 0.15 * consensus_ratio
            avg_conf *= consensus_factor

        # v0.7.10 新增：买入信号成交量确认
        # 缩量买入往往是假突破/诱多，需对置信度打折；卖出允许缩量（恐慌性抛售）
        # v0.9.7 P0 修复：折扣下限从 0.5 放宽到 0.85（极端缩量），
        # 原 0.5 与共识折扣叠乘会把置信度压到门槛以下导致 0 信号。
        volume_factor = self._apply_volume_confirmation(df, direction)
        avg_conf *= volume_factor

        if avg_conf < self._min_confidence:
            logger.info(
                f"{symbol} 信号 {direction} 置信度 {avg_conf:.2f} < {self._min_confidence}，过滤"
            )
            return None

        result = {
            "symbol": symbol,
            "name": name,
            "direction": direction,
            "confidence": round(avg_conf, 3),
            "strategy": ",".join(triggered),
            "strategies": triggered,
            "reason": f"策略 {','.join(triggered)} 触发 {direction} 信号",
        }
        # P1 共振等级：多策略共振过滤（利弗莫尔：关键点需要确认，不依赖单一信号）
        # - resonance >= 2：多策略共振 → 强信号（推送）
        # - resonance == 1：单策略触发 → 弱信号（推送但标注；趋势类单策略仍有效）
        # - conflict：买卖方向同时触发 → 矛盾信号（单独标注，拒绝机械转达）
        n_triggered = len(set(triggered))
        result["resonance"] = n_triggered
        if n_triggered >= 2:
            result["resonance_level"] = "strong"
            result["reason"] += f"（{n_triggered} 策略共振）"
        else:
            result["resonance_level"] = "single"
        if buy_votes and sell_votes:
            result["signal_conflict"] = True
            conflict_buy = [n for n, _ in buy_votes]
            conflict_sell = [n for n, _ in sell_votes]
            result["conflict_detail"] = {
                "buy": conflict_buy,
                "sell": conflict_sell,
            }
            result["reason"] += f" ⚠️ 矛盾信号：{len(conflict_buy)}买 vs {len(conflict_sell)}卖"
        if volume_factor < 1.0:
            result["volume_confirmed"] = False
            result["reason"] += "（缩量，置信度打折）"

        # 附加市场状态信息
        if regime:
            result["regime"] = regime

        # 云策略专属：附加三维趋势数据到 market_context
        if "cloud_trend" in triggered:
            cloud_data = self._extract_cloud_trend_context(df)
            if cloud_data:
                result["market_context"] = cloud_data

        return result

    def _apply_volume_confirmation(self, df: pd.DataFrame, direction: str) -> float:
        """买入信号成交量确认。

        逻辑：
        - 买入需要成交量配合，缩量买入往往为假突破/诱多
        - 卖出不强制放量（恐慌性抛售可能缩量）

        v0.9.7 P0 修复：折扣从 0.5~1.0 放宽到 0.85~1.0——
        原 0.5 档与共识折扣叠乘会把置信度压到门槛以下，导致 30 天 0 信号。
        缩量仅轻度降级（0.85），不再"一票否决"；趋势/共识已承担主要过滤职责。

        返回折扣因子（0.85 ~ 1.0），直接乘到置信度上。
        """
        if direction != "buy" or "volume" not in df.columns or len(df) < 20:
            return 1.0

        try:
            latest_vol = float(df["volume"].iloc[-1])
            vol_ma20 = float(df["volume"].rolling(20).mean().iloc[-1])
            if vol_ma20 <= 0 or latest_vol <= 0:
                return 1.0

            ratio = latest_vol / vol_ma20
            if ratio >= 1.0:
                return 1.0
            if ratio >= 0.8:
                return 0.95
            if ratio >= 0.5:
                return 0.9
            return 0.85
        except Exception:
            return 1.0

    def _extract_cloud_trend_context(self, df: pd.DataFrame) -> dict[str, Any] | None:
        """从云策略输出中提取三维趋势上下文数据。

        云策略在 df 上附加了 dim1_ma_bull / dim2_hl_rising / dim3_vol_healthy
        等列，提取最后一行的值供飞书卡片展示。
        """
        try:
            if df.empty:
                return None
            last = df.iloc[-1]
            required_cols = [
                "dim1_ma_bull",
                "dim2_hl_rising",
                "dim3_vol_healthy",
                "trend_dims_count",
            ]
            if not all(c in df.columns for c in required_cols):
                return None

            context = {
                "dim1_ma_bull": bool(last.get("dim1_ma_bull", False)),
                "dim2_hl_rising": bool(last.get("dim2_hl_rising", False)),
                "dim3_vol_healthy": bool(last.get("dim3_vol_healthy", False)),
                "trend_dims_count": int(last.get("trend_dims_count", 0)),
                "trend_all_3_days": int(last.get("trend_all_3_days", 0)),
            }

            # 判断入场类型
            close = float(last.get("close", 0))
            hh_prev = float(last.get("hh", 0)) if "hh" in df.columns else 0
            ma_trend = float(last.get("ma_trend", 0))
            vol = float(last.get("volume", 0))
            vol_ma = float(last.get("vol_ma", 0))

            if hh_prev > 0 and close > hh_prev and vol > vol_ma * 1.5:
                context["entry_type"] = "breakout"
            elif ma_trend > 0 and close <= ma_trend * 1.01:
                context["entry_type"] = "pullback"
            else:
                context["entry_type"] = ""

            # 双阴反转检测
            is_yin = bool(last.get("is_yin", False))
            prev_yin = (
                bool(df["is_yin"].iloc[-2]) if len(df) >= 2 and "is_yin" in df.columns else False
            )
            prev2_yin = (
                bool(df["is_yin"].iloc[-3]) if len(df) >= 3 and "is_yin" in df.columns else False
            )
            is_yang = bool(last.get("is_yang", False))
            context["double_yin_reversal"] = prev_yin and prev2_yin and (not is_yin or is_yang)

            return context
        except Exception:
            return None
