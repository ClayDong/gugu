"""Centralized stock name lookup table.

Single source of truth for local fallback stock names.
Import from here instead of duplicating in data/manager.py,
engine/main.py, and engine/signal_pipeline.py.
"""

from __future__ import annotations

from functools import lru_cache

# Mutable dict for runtime updates (e.g., from screener or manual adds).
# Keys are 6-digit string codes, values are Chinese stock names.
_STOCK_NAMES: dict[str, str] = {
    "600519": "贵州茅台", "300750": "宁德时代", "000858": "五粮液",
    "601318": "中国平安", "000333": "美的集团", "300059": "东方财富",
    "600030": "中信证券", "000776": "广发证券", "603259": "药明康德",
    "600600": "青岛啤酒", "002625": "光启技术", "600674": "川投能源",
    "688396": "华润微", "601238": "广汽集团", "600460": "士兰微",
    "000977": "浪潮信息", "002049": "紫光国微", "300033": "同花顺",
    "600026": "中远海能", "600150": "中国船舶", "600489": "中金黄金",
    "600584": "长电科技", "601899": "紫金矿业", "603019": "中科曙光",
    "603799": "华友钴业", "600036": "招商银行", "601398": "工商银行",
    "601939": "建设银行", "000538": "云南白药",
    # 扩充常见 A 股（覆盖自选+热门蓝筹+行业龙头）
    "000001": "平安银行", "000002": "万科A", "000063": "中兴通讯",
    "000100": "TCL科技", "000333": "美的集团", "000651": "格力电器",
    "000725": "京东方A", "000895": "双汇发展", "002007": "华兰生物",
    "002027": "分众传媒", "002230": "科大讯飞", "002241": "歌尔股份",
    "002271": "东方雨虹", "002304": "洋河股份", "002415": "海康威视",
    "002475": "立讯精密", "002594": "比亚迪", "002714": "牧原股份",
    "300015": "爱尔眼科", "300027": "华谊兄弟", "300122": "智飞生物",
    "300124": "汇川技术", "300274": "阳光电源", "300316": "晶盛机电",
    "438001": "华夏沪深300ETF", "510300": "沪深300ETF",
    "159919": "嘉实300", "510050": "50ETF",
    "600000": "浦发银行", "600009": "上海机场", "600016": "民生银行",
    "600028": "中国石化", "600029": "南方航空", "600031": "三一重工",
    "600048": "保利发展", "600050": "中国联通", "600085": "同仁堂",
    "600089": "特变电工", "600104": "上汽集团", "600196": "复星医药",
    "600276": "恒瑞医药", "600346": "恒力石化", "600406": "国电南瑞",
    "600438": "通威股份", "600519": "贵州茅台", "600585": "海螺水泥",
    "600588": "用友网络", "600690": "海尔智家", "600745": "闻泰科技",
    "600809": "山西汾酒", "600886": "国投电力", "600887": "伊利股份",
    "600900": "长江电力", "601006": "大秦铁路", "601088": "中国神华",
    "601111": "中国国航", "601138": "工业富联", "601166": "兴业银行",
    "601169": "北京银行", "601225": "陕西煤业", "601228": "广州港",
    "601288": "农业银行", "601318": "中国平安", "601328": "交通银行",
    "601333": "广深铁路", "601336": "新华保险", "601390": "中国中铁",
    "601528": "瑞茂通", "601601": "中国太保", "601618": "中国中冶",
    "601628": "中国人寿", "601633": "长城汽车", "601668": "中国建筑",
    "601688": "华泰证券", "601728": "中国电信", "601766": "中国中车",
    "601788": "光大证券", "601800": "中国交建", "601818": "光大银行",
    "601857": "中国石油", "601888": "中国中免", "601919": "中远海控",
    "601988": "中国银行", "601989": "中国重工", "601998": "中信银行",
    "603160": "汇顶科技", "603501": "韦尔股份", "603799": "华友钴业",
    "603986": "兆易创新", "688005": "容百科技", "688008": "澜起科技",
    "688012": "中微公司", "688036": "传音控股", "688111": "金山办公",
    "688126": "沪硅产业", "688185": "康希诺", "688200": "华峰测控",
    "688202": "美迪西", "688223": "晶合集成", "688256": "寒武纪",
    "688272": "富吉瑞", "688303": "大全能源", "688363": "华熙生物",
    "688396": "华润微", "688567": "孚能科技", "688599": "天合光能",
}


@lru_cache(maxsize=1)
def get_stock_names() -> dict[str, str]:
    """Return a frozen copy of the static fallback name table."""
    return dict(_STOCK_NAMES)


def lookup_stock_name(code: str) -> str:
    """Look up a stock name by 6-digit code. Returns empty string on miss."""
    return _STOCK_NAMES.get(code.strip().zfill(6), "")


def register_stock_name(code: str, name: str) -> None:
    """Register or update a stock name mapping at runtime."""
    _STOCK_NAMES[code.strip().zfill(6)] = name


def load_names_from_history(filepath: str = "") -> int:
    """从 screener_history.jsonl 加载历史股票名称到映射表。

    尾盘选股扫描时已经获取了股票名称，可以复用作为 fallback。
    Returns 注册的名称数量。
    """
    import json
    import os

    if not filepath:
        # 默认路径：data/screener_history.jsonl
        filepath = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "data",
            "screener_history.jsonl",
        )
    if not os.path.exists(filepath):
        return 0
    count = 0
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    symbol = d.get("symbol", "")
                    name = d.get("name", "")
                    # symbol 格式可能是 "sz000001" 或 "000001"
                    code = symbol[-6:] if len(symbol) >= 6 else symbol
                    if code and name and name != code:
                        existing = _STOCK_NAMES.get(code)
                        if not existing:
                            _STOCK_NAMES[code] = name
                            count += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass
    return count
