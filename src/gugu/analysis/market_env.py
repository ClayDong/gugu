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
        sealed_count = 0  # 封板数（涨停且未炸板，即收阳）
        relay_candidates = 0  # 接力候选（前日涨停的股票数）
        relay_success = 0  # 接力成功（前日涨停 + 今日高开）
        valid_samples = 0

        for _sym, df in watchlist_data.items():
            if len(df) < 3:
                continue
            valid_samples += 1
            close = df["close"].values
            open_p = df["open"].values
            daily_ret = (close[-1] / close[-2] - 1) if close[-2] > 0 else 0
            prev_ret = (close[-2] / close[-3] - 1) if len(close) >= 3 and close[-3] > 0 else 0

            # 涨停检测（涨幅 > 9.5%）
            if daily_ret > 0.095:
                limit_up_count += 1
                # 封板：涨停且收阳
                if close[-1] > open_p[-1]:
                    sealed_count += 1

            # 接力检测：前日涨停 + 今日高开
            if prev_ret > 0.095:
                relay_candidates += 1
                if open_p[-1] > close[-2]:  # 高开
                    relay_success += 1

        seal_rate = sealed_count / limit_up_count if limit_up_count > 0 else 0
        relay_rate = relay_success / relay_candidates if relay_candidates > 0 else 0

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
            "seal_rate": round(seal_rate, 2),
            "relay_rate": round(relay_rate, 2),
            "ice_point": is_ice,
            "mode": mode,
            "advice": advice,
            "can_board": seal_rate >= self.min_seal_rate,
            "can_relay": relay_rate >= self.min_relay_rate,
        }
        logger.info(
            "市场环境: 涨停=%d 封板率=%.0f%% 接力率=%.0f%% 模式=%s",
            limit_up_count,
            seal_rate * 100,
            relay_rate * 100,
            mode,
        )
        return result


def should_empty_position(env: dict) -> bool:
    """是否应该空仓。"""
    return env.get("mode") == "empty"


def should_avoid_board(env: dict) -> bool:
    """是否应该避免打板。"""
    return not env.get("can_board", False)


def should_avoid_relay(env: dict) -> bool:
    """是否应该避免接力。"""
    return not env.get("can_relay", False)
