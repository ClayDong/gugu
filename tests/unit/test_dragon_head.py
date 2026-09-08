from __future__ import annotations

import pandas as pd

from gugu.analysis.dragon_head import DragonHeadDetector


def _df(offset: float = 0.0) -> pd.DataFrame:
    close = [100.0 + offset + i * 0.1 for i in range(30)]
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=30),
            "open": close,
            "high": [x * 1.01 for x in close],
            "low": [x * 0.99 for x in close],
            "close": close,
            "volume": [1_000_000] * 30,
            "amount": [100_000_000] * 30,
        }
    )


def test_detector_adds_unverified_evidence_fields():
    result = DragonHeadDetector(sector_map={"test": ["600519", "000858"]}).detect(
        {"600519": _df(), "000858": _df(1.0)}
    )
    assert result
    assert all(item["evidence_status"] == "heuristic" for item in result.values())
    assert all("asof" in item and "data_coverage" in item for item in result.values())


def test_detector_is_empty_when_sector_coverage_insufficient():
    result = DragonHeadDetector(sector_map={"test": ["600519", "000858"]}).detect({"600519": _df()})
    assert result == {}
