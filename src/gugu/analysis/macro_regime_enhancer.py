"""宏观数据增强器：将五维宏观数据（金/油/汇/债/G）注入市场状态判断。

从 gugu.macro 模块采集宏观数据，调整 RegimeDetector 的输出：
- 黄金暴涨（避险模式）→ 降低仓位上限
- 人民币贬值 → 降低仓位上限（资本外流风险）
- 美债收益率飙升 → 降低仓位上限（流动性收紧）
- 美债倒挂 → 经济衰退风险，大幅降低仓位上限
- VIX 恐慌 → 降低仓位上限
"""

from __future__ import annotations

from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

# 宏观因子阈值与调整幅度
_MACRO_RULES: list[tuple[str, str, float, float, str]] = [
    # (field_key, sub_key, threshold, reduction, description)
    # 黄金 > $3000 避险模式
    ("gold", "gold_price", 3000.0, 0.10, "黄金暴涨({val:.0f}$)，避险模式"),
    # 人民币 > 7.2 资本外流
    ("fx", "usd_cny", 7.2, 0.10, "人民币贬值({val:.2f})，资本外流风险"),
    # 人民币 > 7.5 严重贬值
    ("fx", "usd_cny", 7.5, 0.20, "人民币严重贬值({val:.2f})，大幅降仓"),
    # 美债10Y > 5% 流动性收紧
    ("bond", "us10y_yield", 5.0, 0.10, "美债收益率飙升({val:.2f}%)，流动性收紧"),
    # 美债10Y > 4.5% 预警
    ("bond", "us10y_yield", 4.5, 0.05, "美债收益率偏高({val:.2f}%)，谨慎"),
    # 美债倒挂
    ("bond", "inverted", True, 0.20, "美债收益率曲线倒挂，衰退风险"),
    # VIX > 30 恐慌
    ("derivative", "vix", 30.0, 0.15, "VIX恐慌指数{val:.1f}，极端波动"),
    # VIX > 25 预警
    ("derivative", "vix", 25.0, 0.10, "VIX偏高({val:.1f})，波动加大"),
]


class MacroRegimeEnhancer:
    """宏观数据增强器：从 macro 模块采集数据，调整市场判断。"""

    def __init__(self) -> None:
        self._macro_snapshot: dict[str, Any] | None = None

    async def enhance(self, regime_result: dict[str, Any]) -> dict[str, Any]:
        """增强市场状态判断。

        Args:
            regime_result: RegimeDetector.detect() 的原始输出。

        Returns:
            含 ``macro_adjustment`` 字段的增强结果。
        """
        # 仅在没有预置数据时采集（方便测试注入）
        if self._macro_snapshot is None:
            await self._collect_macro_data()
        if not self._macro_snapshot:
            # 无宏观数据可用时，返回原始结果并标注原因
            return {**regime_result, "macro_adjustment": {
                "reasons": [], "reduction": 0.0, "note": "宏观数据不可用",
            }}

        reasons: list[str] = []
        total_reduction = 0.0

        for dim, key, threshold, reduction, desc in _MACRO_RULES:
            val = self._resolve_value(dim, key)
            if val is None:
                continue

            # 阈值比较
            if isinstance(threshold, bool):
                triggered = bool(val) is threshold
            elif isinstance(threshold, (int, float)):
                triggered = float(val) >= float(threshold)
            else:
                triggered = False

            if triggered:
                formatted_desc = desc.format(
                    val=float(val) if not isinstance(val, bool) else val
                )
                reasons.append(formatted_desc)
                total_reduction = max(total_reduction, reduction)

        # 最多降低 30%
        total_reduction = min(total_reduction, 0.30)

        if total_reduction > 0:
            old_limit = regime_result.get("total_position_limit", 0.40)
            new_limit = max(old_limit - total_reduction, 0.0)
            logger.info(
                f"宏观增强: 仓位上限 {old_limit:.0%} → {new_limit:.0%}"
                f" ({'; '.join(reasons)})"
            )

            return {
                **regime_result,
                "total_position_limit": new_limit,
                "macro_adjustment": {
                    "reasons": reasons,
                    "reduction": total_reduction,
                    "old_limit": old_limit,
                    "new_limit": new_limit,
                },
            }

        return {
            **regime_result,
            "macro_adjustment": {
                "reasons": [],
                "reduction": 0.0,
            },
        }

    async def _collect_macro_data(self) -> None:
        """从 macro 模块采集宏观数据。"""
        try:
            from gugu.macro.collectors import MacroCollector

            collector = MacroCollector()
            snapshot = await collector.collect()
            self._macro_snapshot = snapshot.to_dict() if hasattr(snapshot, "to_dict") else {}
        except Exception as e:
            logger.debug(f"宏观数据采集失败: {e}")
            self._macro_snapshot = {}

    def _resolve_value(self, dim: str, key: str) -> Any:
        """从宏观字典中解析值。"""
        if not self._macro_snapshot:
            return None
        dim_data = self._macro_snapshot.get(dim, {})
        if not isinstance(dim_data, dict):
            return None
        return dim_data.get(key)
