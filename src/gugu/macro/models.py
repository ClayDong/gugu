"""宏观五维数据模型。

每个维度的数据模型，含合理性校验（来源 MingCe 的 _validate_macro_price）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GoldData:
    """贵金属：黄金、白银"""

    gold_price: float = 0.0  # COMEX 黄金，美元/盎司
    gold_change_pct: float = 0.0
    silver_price: float = 0.0  # COMEX 白银
    silver_change_pct: float = 0.0
    gold_silver_ratio: float = 0.0  # 金银比
    source: str = ""
    valid: bool = False


@dataclass
class OilData:
    """原油：布伦特、WTI"""

    brent_price: float = 0.0  # ICE 布伦特，美元/桶
    brent_change_pct: float = 0.0
    wti_price: float = 0.0  # NYMEX WTI
    wti_change_pct: float = 0.0
    spread: float = 0.0  # 布伦特-WTI 价差
    source: str = ""
    valid: bool = False


@dataclass
class FxData:
    """外汇：美元指数、USDCNY、EUR/USD"""

    usd_index: float = 0.0  # 美元指数
    usd_index_change_pct: float = 0.0
    usd_cny: float = 0.0  # 美元/人民币
    usd_cny_change_pct: float = 0.0
    eur_usd: float = 0.0  # 欧元/美元
    eur_usd_change_pct: float = 0.0
    usd_jpy: float = 0.0  # 美元/日元
    source: str = ""
    valid: bool = False


@dataclass
class BondData:
    """债券：美债收益率、LPR、Shibor"""

    us10y_yield: float = 0.0  # 美国 10 年期国债收益率
    us2y_yield: float = 0.0  # 美国 2 年期
    us10y_2y_spread: float = 0.0  # 期限利差（10Y-2Y）
    inverted: bool = False  # 倒挂标志
    lpr_1y: float = 0.0  # LPR 1 年期
    lpr_5y: float = 0.0  # LPR 5 年期
    shibor_7d: float = 0.0  # Shibor 7 天
    source: str = ""
    valid: bool = False


@dataclass
class DerivativeData:
    """衍生品与综合：VIX、BDI、BTC、北向资金"""

    vix: float = 0.0  # 恐慌指数
    vix_change_pct: float = 0.0
    bdi: float = 0.0  # 波罗的海干散货指数
    bdi_change_pct: float = 0.0
    btc_price: float = 0.0  # 比特币
    btc_change_pct: float = 0.0
    eth_price: float = 0.0  # 以太坊
    eth_change_pct: float = 0.0
    north_flow: float | None = None  # 北向资金当日净流入（亿元），None 表示获取不到
    source: str = ""
    valid: bool = False


@dataclass
class StockIndexData:
    """A 股大盘指数：上证指数、深证成指、创业板指"""

    sh_index: float = 0.0  # 上证指数
    sh_change_pct: float = 0.0
    sh_close: float = 0.0  # 昨收
    sz_index: float = 0.0  # 深证成指
    sz_change_pct: float = 0.0
    sz_close: float = 0.0
    cyb_index: float = 0.0  # 创业板指
    cyb_change_pct: float = 0.0
    cyb_close: float = 0.0
    # 市场情绪指标
    total_volume: float = 0.0  # 沪深两市总成交额（亿元）
    total_turnover: float = 0.0  # 换手率
    limit_up_count: int = 0  # 涨停家数
    limit_down_count: int = 0  # 跌停家数
    source: str = ""
    valid: bool = False


@dataclass
class MacroSnapshot:
    """一次采集的完整宏观快照"""

    gold: GoldData = field(default_factory=GoldData)
    oil: OilData = field(default_factory=OilData)
    fx: FxData = field(default_factory=FxData)
    bond: BondData = field(default_factory=BondData)
    derivative: DerivativeData = field(default_factory=DerivativeData)
    stock_index: StockIndexData = field(default_factory=StockIndexData)
    timestamp: str = ""
    all_valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "gold": {
                "gold_price": self.gold.gold_price,
                "gold_change_pct": self.gold.gold_change_pct,
                "silver_price": self.gold.silver_price,
                "silver_change_pct": self.gold.silver_change_pct,
                "gold_silver_ratio": self.gold.gold_silver_ratio,
                "valid": self.gold.valid,
            },
            "oil": {
                "brent_price": self.oil.brent_price,
                "brent_change_pct": self.oil.brent_change_pct,
                "wti_price": self.oil.wti_price,
                "wti_change_pct": self.oil.wti_change_pct,
                "spread": self.oil.spread,
                "valid": self.oil.valid,
            },
            "fx": {
                "usd_index": self.fx.usd_index,
                "usd_index_change_pct": self.fx.usd_index_change_pct,
                "usd_cny": self.fx.usd_cny,
                "usd_cny_change_pct": self.fx.usd_cny_change_pct,
                "eur_usd": self.fx.eur_usd,
                "eur_usd_change_pct": self.fx.eur_usd_change_pct,
                "valid": self.fx.valid,
            },
            "bond": {
                "us10y_yield": self.bond.us10y_yield,
                "us2y_yield": self.bond.us2y_yield,
                "us10y_2y_spread": self.bond.us10y_2y_spread,
                "inverted": self.bond.inverted,
                "lpr_1y": self.bond.lpr_1y,
                "lpr_5y": self.bond.lpr_5y,
                "shibor_7d": self.bond.shibor_7d,
                "valid": self.bond.valid,
            },
            "derivative": {
                "vix": self.derivative.vix,
                "vix_change_pct": self.derivative.vix_change_pct,
                "bdi": self.derivative.bdi,
                "bdi_change_pct": self.derivative.bdi_change_pct,
                "btc_price": self.derivative.btc_price,
                "btc_change_pct": self.derivative.btc_change_pct,
                "eth_price": self.derivative.eth_price,
                "north_flow": self.derivative.north_flow,
                "valid": self.derivative.valid,
            },
            "stock_index": {
                "sh_index": self.stock_index.sh_index,
                "sh_change_pct": self.stock_index.sh_change_pct,
                "sh_close": self.stock_index.sh_close,
                "sz_index": self.stock_index.sz_index,
                "sz_change_pct": self.stock_index.sz_change_pct,
                "sz_close": self.stock_index.sz_close,
                "cyb_index": self.stock_index.cyb_index,
                "cyb_change_pct": self.stock_index.cyb_change_pct,
                "cyb_close": self.stock_index.cyb_close,
                "total_volume": self.stock_index.total_volume,
                "limit_up_count": self.stock_index.limit_up_count,
                "limit_down_count": self.stock_index.limit_down_count,
                "valid": self.stock_index.valid,
            },
            "timestamp": self.timestamp,
        }
