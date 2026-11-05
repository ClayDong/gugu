"""市场环境检测器（MarketEnvironment）。

钱多多散户生存法则：
- 如果整个市场每天只有几只涨停板了，那就空仓等机会
- 封板率低于70%的市场环境就不打板
- 接力成功率低于60%的环境就不接力
- 什么时候出现冰点的极致，买在不得不涨的时间窗口
- 个股先于情绪，情绪先于板块，板块先于大盘
- 不要预判大盘，做大盘的跟随者

散户的生存优势：
- 可以空仓（机构有仓位限制）
- 可以只做一只股票（机构有分散要求）
- 可以等冰点（机构有赎回压力）
- 没有业绩排名压力
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class MarketEnvironment:
    """市场环境检测器。

    判断当前是否适合交易，以及适合哪种交易模式。
    不预判，只跟随——大盘是什么状态，就做什么动作。
    """

    def __init__(
        self,
        # 冰点阈值
        ice_point_limit: int = 5,  # 涨停数 < 此值 = 冰点
        ice_point_sentiment: float = 0.2,  # 情绪 < 此值 = 冰点
        # 打板环境
        min_seal_rate: float = 0.70,  # 封板率最低要求
        # 接力环境
        min_relay_rate: float = 0.60,  # 接力成功率最低要求
        # 正常环境
        normal_limit_count: int = 20,  # 涨停数 > 此值 = 正常环境
    ):
        self.ice_point_limit = ice_point_limit
        self.ice_point_sentiment = ice_point_sentiment
        self.min_seal_rate = min_seal_rate
        self.min_relay_rate = min_relay_rate
        self.normal_limit_count = normal_limit_count

    def assess(
        self,
        watchlist_data: dict[str, pd.DataFrame],
        sentiment: float | None = None,
    ) -> dict:
        """评估当前市场环境。

        Args:
            watchlist_data: {symbol: DataFrame} 所有自选股数据
            sentiment: 当前情绪指标（0-1，1=极度贪婪）

        Returns:
            {
                "limit_up_count": 涨停数,
                "seal_rate": 封板率,
                "relay_rate": 接力成功率,
                "ice_point": 是否冰点,
                "mode": "ice_point_buy" | "normal" | "empty",
                "advice": 操作建议,
                "can_board": 是否可以打板,
                "can_relay": 是否可以接力,
            }
        """
        limit_up_count = 0  # 涨停数
        sealed_count = 0  # 收盘封住涨停的家数（盘中曾涨停的子集）
        touched_limit_count = 0  # 盘中曾触及涨停的家数（封板率的分母）
        relay_candidates = 0  # 接力候选（前日涨停的股票数）
        relay_success = 0  # 接力成功（前日涨停 + 今日高开）
        valid_samples = 0

        for _sym, df in watchlist_data.items():
            if len(df) < 3:
                continue
            valid_samples += 1
            close = df["close"].values
            open_p = df["open"].values
            high = df["high"].values if "high" in df else close
            daily_ret = (close[-1] / close[-2] - 1) if close[-2] > 0 else 0
            prev_ret = (close[-2] / close[-3] - 1) if len(close) >= 3 and close[-3] > 0 else 0

            # 涨停价近似（沿用 9.5% 口径，与下方 limit_up_count 判定保持一致）
            limit_price = close[-2] * 1.095 if close[-2] > 0 else 0.0
            touched_limit = limit_price > 0 and high[-1] >= limit_price
            closed_at_limit = limit_price > 0 and close[-1] >= limit_price

            # 涨停检测（收盘涨幅 > 9.5%）——语义保持不变：用于冰点/空仓判断
            if daily_ret > 0.095:
                limit_up_count += 1

            # 封板率：分母是「盘中曾涨停」，分子是「收盘仍封在涨停价」。
            # 原实现用 close > open 判封板，把一字板（open == close == 涨停价）
            # 判成「未封板」，反而让最强的形态被打板风控否决——方向是反的。
            # 炸板（盘中触及涨停但收盘回落）才是真正的「未封板」。
            if touched_limit:
                touched_limit_count += 1
                if closed_at_limit:
                    sealed_count += 1

            # 接力检测：前日涨停 + 今日高开
            if prev_ret > 0.095:
                relay_candidates += 1
                if open_p[-1] > close[-2]:  # 高开
                    relay_success += 1

        # 封板率/接力率是「比率型」指标：分母为 0 意味着没有可判断的样本，
        # 而不是「成功率为 0」。原实现用 ``else 0`` 把「无样本」压成 0%，
        # 再经 ``can_relay = 0 >= 0.6`` 判为 False，最终由 should_avoid_relay
        # 否决信号——这是把「不知道」当成「不合格」，属于逻辑错误。
        #
        # 2026-11-05 实证：data/trade_journal.jsonl 144 条记录 100% 被拦，
        # 其中 45 条明确写「接力成功率0%低于60%」。自选股池只有 18 只，
        # 「前日涨停」的候选长期为 0，于是该指标恒为 0、拦截永久生效，
        # 策略信号永远进不了成交。改为 None 表示不可判断。
        seal_rate = sealed_count / touched_limit_count if touched_limit_count > 0 else None
        relay_rate = relay_success / relay_candidates if relay_candidates > 0 else None

        # ── 冰点判断 ──────────────────────────────────
        is_ice = (limit_up_count < self.ice_point_limit) or (
            sentiment is not None and sentiment < self.ice_point_sentiment
        )

        # ── 环境模式 ──────────────────────────────────
        if valid_samples < 3:
            mode = "unavailable"
            advice = "市场环境数据不足，仅观察，不产生可执行买入。"
        elif is_ice:
            # 冰点 → 试探性买入（买在不得不涨的时间窗口）
            mode = "ice_point_buy"
            advice = "冰点极致，个股先于情绪，情绪先于板块。关注率先涨停的焦点股。"
        elif limit_up_count < self.normal_limit_count:
            # 涨停稀少 → 空仓
            mode = "empty"
            advice = "每天只有几只涨停板，空仓等机会。"
        else:
            mode = "normal"
            advice = "正常环境，按策略信号执行。"

        result = {
            "detector_status": "available" if valid_samples >= 3 else "insufficient",
            "asof": datetime.now().isoformat(timespec="seconds"),
            "sample_count": valid_samples,
            "limit_up_count": limit_up_count,
            "touched_limit_count": touched_limit_count,
            "relay_candidate_count": relay_candidates,
            # None = 无样本，无法计算比率；调用方必须区分「无法判断」与「0 分」
            "seal_rate": None if seal_rate is None else round(seal_rate, 2),
            "relay_rate": None if relay_rate is None else round(relay_rate, 2),
            "ice_point": is_ice,
            "mode": mode,
            "advice": advice,
            "can_board": None if seal_rate is None else seal_rate >= self.min_seal_rate,
            "can_relay": None if relay_rate is None else relay_rate >= self.min_relay_rate,
        }
        logger.info(
            "市场环境: 涨停={} 封板率={} 接力率={} 模式={}",
            limit_up_count,
            "n/a" if seal_rate is None else f"{seal_rate:.0%}",
            "n/a" if relay_rate is None else f"{relay_rate:.0%}",
            mode,
        )
        return result


def should_empty_position(env: dict) -> bool:
    """是否应该空仓。"""
    return env.get("mode") == "empty"


def should_avoid_board(env: dict) -> bool:
    """是否应该避免打板。

    ``can_board`` 为 None 表示「池内没有涨停样本，封板率无法计算」。
    此时不做否决——用无样本的伪 0 分去否决信号，会让策略长期无法交易。
    """
    can_board = env.get("can_board")
    if can_board is None:
        return False
    return not can_board


def should_avoid_relay(env: dict) -> bool:
    """是否应该避免接力。

    ``can_relay`` 为 None 表示「池内无前日涨停股，接力成功率无法计算」。
    同理不做否决；有样本且低于阈值时仍然拦截。
    """
    can_relay = env.get("can_relay")
    if can_relay is None:
        return False
    return not can_relay
