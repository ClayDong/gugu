"""尾盘选股参数管理。

支持从 data/screener_params.json 加载运行时参数，并提供自动调优后的
原子保存接口。所有阈值都有硬边界，防止调优过程把参数推到非理性区间。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gugu.utils.atomic import atomic_write_text
from gugu.utils.log import get_logger

logger = get_logger()

_DEFAULT_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
_PARAMS_FILE = _DEFAULT_DATA_DIR / "screener_params.json"


@dataclass
class ScreenerParams:
    """尾盘选股运行时参数。

    默认值为当前经回测/实战验证的保守阈值，自动调优只会在
    `_BOUNDS` 范围内小幅修正。
    """

    # 涨幅区间（低吸逻辑）
    # v0.7.9 优化：max_chg_pct +1%→+2%（原值在震荡市过滤了太多真正的强势股回踩）
    min_chg_pct: float = -1.5
    max_chg_pct: float = 2.0

    # 前 N 日强势表现
    # v0.7.9 优化：窗口5天→10天（更长的回看能筛选出真正的强势股）
    recent_strong_pct: float = 4.0
    recent_strong_days: int = 10

    # 量比上限（<1 表示缩量）
    # v0.7.9 优化：1.0→1.2（适度放宽，区分缩量回踩和缩量无人问津）
    max_vol_ratio: float = 1.2

    # 换手率区间
    # v0.7.9 优化：下限3%→2%（2-3%换手的好股被过滤了太多）
    min_turnover: float = 2.0
    max_turnover: float = 15.0

    # 流通市值区间（元）
    min_mcap: float = 5_000_000_000.0
    max_mcap: float = 50_000_000_000.0

    # 最多返回几只
    max_candidates: int = 10

    # 市场状态过滤：这些 regime 下跳过正式选股
    skip_regimes: list[str] = field(default_factory=lambda: ["bear", "crash"])

    # 不同 regime 下的候选数量修正系数
    candidate_modifier: dict[str, float] = field(
        default_factory=lambda: {
            "bull": 1.0,
            "rally": 1.0,
        }
    )

    # 自动调优开关
    auto_tune_enabled: bool = True

    # 调优回看窗口（交易日）
    tune_lookback_days: int = 10

    # 调优触发阈值
    # v0.7.9 优化：收紧阈值从-0.5%/30%改为-0.3%/25%（原值太严，实测从未触发过调优）
    tune_tighten_open_excess: float = -0.3
    tune_tighten_win_rate: float = 0.25
    tune_tighten_stop_loss_rate: float = 0.40

    # 放宽触发的条件
    # v0.7.9 优化：放宽阈值从0.3%/45%改为0.2%/40%（原值几乎无法达到）
    tune_loosen_open_excess: float = 0.20
    tune_loosen_win_rate: float = 0.40

    # 参数硬边界（自动调优不得越界）
    _BOUNDS: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "min_chg_pct": (-5.0, -1.0),
            "max_chg_pct": (-1.0, 2.0),
            "max_vol_ratio": (0.5, 1.5),
            "min_turnover": (1.0, 5.0),
            "max_turnover": (10.0, 25.0),
            "max_candidates": (3, 15),
        },
        repr=False,
    )

    def effective_max_candidates(self, regime: str) -> int:
        """根据市场状态计算实际最大候选数。

        bear/crash 等 modifier 为 0 的 regime 返回 0，与 should_skip 保持一致。
        """
        modifier = self.candidate_modifier.get(regime, self.candidate_modifier.get("sideways", 0.6))
        if modifier <= 0:
            return 0
        return max(1, round(self.max_candidates * modifier))

    def should_skip(self, regime: str) -> bool:
        """当前 regime 是否需要跳过正式选股。"""
        return regime in self.skip_regimes

    def apply_adjustment(self, adjustments: dict[str, float]) -> ScreenerParams:
        """应用自动调优产生的增量，并裁剪到边界内。

        Args:
            adjustments: 参数名 -> 增量（如 {"max_vol_ratio": -0.05}）

        Returns:
            新的 ScreenerParams 实例（原实例不变）
        """
        data = asdict(self)
        for key, delta in adjustments.items():
            if key not in data:
                continue
            data[key] = data[key] + delta

        # 裁剪到硬边界
        for key, (low, high) in self._BOUNDS.items():
            if key in data:
                data[key] = max(low, min(high, data[key]))

        # 重新构造，去掉内部字段
        data.pop("_BOUNDS", None)
        return ScreenerParams(**data)

    def to_dict(self) -> dict[str, Any]:
        """导出为 dict，排除内部字段（如 _BOUNDS）。"""
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}

    @classmethod
    def load(cls, path: Path | str | None = None) -> ScreenerParams:
        """从 JSON 文件加载参数；文件不存在或损坏时返回默认值。"""
        file_path = Path(path) if path else _PARAMS_FILE
        if not file_path.exists():
            return cls()
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            # 只使用合法字段
            valid_keys = {
                f.name for f in cls.__dataclass_fields__.values() if not f.name.startswith("_")
            }
            filtered = {k: v for k, v in data.items() if k in valid_keys}
            return cls(**filtered)
        except Exception as e:
            logger.warning("加载尾盘选股参数失败，使用默认值: {}", e)
            return cls()

    def save(self, path: Path | str | None = None) -> None:
        """原子写入参数文件。"""
        file_path = Path(path) if path else _PARAMS_FILE
        os.makedirs(file_path.parent, exist_ok=True)
        atomic_write_text(file_path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
