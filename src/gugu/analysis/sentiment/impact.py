"""股价影响因子分析引擎：舆情影响评分 + 历史案例 + 时效性衰减。

核心逻辑：
1. 媒体权重：不同来源（证券时报 1.5x，东方财富 1.2x）
2. 历史影响系数：正向平均 +1.5%，负向平均 -1.2%
3. 时效性指数衰减：1d→0.9, 5d→0.67
4. 综合影响分 = 情感得分 × 情感强度 × 媒体权重
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

_DEFAULT_HISTORY = {
    "historical_cases": [],
    "impact_statistics": {
        "positive": {"day_1": 0.015, "day_3": 0.025, "day_5": 0.035, "day_10": 0.040},
        "negative": {"day_1": -0.012, "day_3": -0.022, "day_5": -0.030, "day_10": -0.035},
    },
    "media_weights": {
        "证券时报": 1.5,
        "中国证券报": 1.5,
        "上海证券报": 1.5,
        "证券日报": 1.5,
        "21世纪经济报道": 1.4,
        "第一财经": 1.4,
        "东方财富网": 1.2,
        "新浪财经": 1.1,
        "搜狐财经": 1.0,
        "网易财经": 1.0,
    },
}


class ImpactAnalyzer:
    """股价影响因子分析引擎。"""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parents[4]
        self._data_dir = Path(data_dir) if data_dir else root / "data" / "sentiment"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._history_path = self._data_dir / "sentiment_history.json"
        self._init_db()

    def _init_db(self) -> None:
        if not self._history_path.exists():
            self._history_path.write_text(
                json.dumps(_DEFAULT_HISTORY, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(f"已创建舆情历史库: {self._history_path}")

    def _load(self) -> dict:
        try:
            return json.loads(self._history_path.read_text(encoding="utf-8"))
        except Exception:
            return dict(_DEFAULT_HISTORY)

    def _save(self, data: dict) -> None:
        try:
            self._history_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("保存舆情历史失败: {}", e)

    def calculate(self, sentiment_result: dict, source: str = "未知") -> dict[str, Any]:
        """计算舆情影响分数。"""
        score = sentiment_result.get("sentiment_score", 0)
        strength = sentiment_result.get("sentiment_strength", 1)
        history = self._load()
        media_weight = history.get("media_weights", {}).get(source, 1.0)
        sentiment = sentiment_result.get("sentiment", "neutral")
        stats = history.get("impact_statistics", {})
        h = stats.get(sentiment, {"day_1": 0}) if sentiment != "neutral" else {"day_1": 0}
        overall = score * strength * media_weight
        decay = {f"day_{d}": math.exp(-0.1 * (d - 1)) for d in [1, 3, 5, 10]}
        return {
            "overall_impact_score": round(overall, 4),
            "media_weight": media_weight,
            "time_horizon_impact": {
                f"day_{d}": round(overall * h.get(f"day_{d}", 0), 4) for d in [1, 3, 5, 10]
            },
            "time_decay_factors": decay,
        }

    def add_case(self, news: dict, sentiment: dict) -> None:
        """添加历史案例。"""
        data = self._load()
        cases = data.setdefault("historical_cases", [])
        cases.append(
            {
                "id": len(cases) + 1,
                "date": str(__import__("datetime").date.today()),
                "title": news.get("title", ""),
                "source": news.get("source", ""),
                "sentiment": sentiment.get("sentiment", "neutral"),
                "sentiment_score": sentiment.get("sentiment_score", 0),
                "key_words": sentiment.get("key_words", []),
            }
        )
        data["historical_cases"] = cases[-100:]
        self._save(data)
