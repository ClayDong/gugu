"""小圈圈儿《反包策略》V2.0 战法规则包。

对应《反包策略》广东经济出版社，2025年6月
覆盖 5+1 种战法（完整原书修正版 V2.0）。
"""

from gugu.strategies.fanbao.rules import (  # noqa: F401
    RULE_VERSION,
    FanbaoSignal,
    check_general_conditions,
    check_red_lines,
    detect_all,
    detect_baoli_xipan,
    detect_callback_fanbao,
    detect_dieting_fanbao,
    detect_sentiment_cycle,
    detect_tiaokong_fanbao,
    detect_yizi_fanbao,
    detect_zhaban_fanbao,
    enrich,
)

__all__ = [
    "RULE_VERSION",
    "FanbaoSignal",
    "check_red_lines",
    "check_general_conditions",
    "detect_sentiment_cycle",
    "detect_dieting_fanbao",
    "detect_zhaban_fanbao",
    "detect_yizi_fanbao",
    "detect_callback_fanbao",
    "detect_tiaokong_fanbao",
    "detect_baoli_xipan",
    "detect_all",
    "enrich",
]
