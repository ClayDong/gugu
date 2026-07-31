"""另类 Alpha 因子过滤器 — 选股信号增强（数据缺失时默认放行）。

将已建成的 8 个另类因子接入信号流水线，为买入信号提供额外维度评分：
- 高管净增持、龙虎榜机构净买、融资余额变化、筹码集中度等事件驱动/行为金融信号
- 仅作为**信号增强器**：高评分提升 confidence，极低评分标记过滤理由
- 数据缺失或采集失败时**默认放行**（fail-open），不阻塞已有策略信号

与 AlphaFactory 的关系：
- 本过滤器调用 AltAlphaFactorEngine 获取横截面 z-score
- 再经 AlphaFactory.compute_altalpha_factors / composite_score_with_altalpha 融合为综合评分
"""

from __future__ import annotations

from datetime import date
from typing import Any

from gugu.analysis.alpha_factory import AlphaFactory
from gugu.analysis.altalpha_factors import AltAlphaFactorEngine
from gugu.utils.log import get_logger

logger = get_logger()


class AltAlphaFilter:
    """另类 Alpha 因子过滤器。

    阈值（可通过 settings.yaml ``altalpha_filter`` 段覆盖）：
        enabled: 是否启用（默认 true）
        block_threshold: 综合评分低于此值时标记过滤（默认 -0.50，较严格）
        boost_threshold: 综合评分高于此值时提升 confidence（默认 0.30）
        confidence_boost: 高评分时的 confidence 增量（默认 0.10）
        altalpha_weight: 另类因子在综合评分中的权重（默认 0.30）
        fetch_financial: 是否获取财务数据（较慢，默认 false）

    数据缺失处理：
    - 采集失败或因子全为 0 时默认放行（pass=True），reasons 标记"另类因子数据不可用"
    - 另类数据是事件驱动变量，不应因数据缺失而拒绝整个信号
    """

    DEFAULT_BLOCK_THRESHOLD = -0.50
    DEFAULT_BOOST_THRESHOLD = 0.30
    DEFAULT_CONFIDENCE_BOOST = 0.10
    DEFAULT_ALTPHA_WEIGHT = 0.30
    DEFAULT_FETCH_FINANCIAL = False

    def __init__(self) -> None:
        # 当日缓存：{symbol: (date, result_dict)}
        self._cache: dict[str, tuple[date, dict[str, Any]]] = {}
        # 延迟初始化引擎（避免启动时即请求网络）
        self._engine: AltAlphaFactorEngine | None = None
        self._factory = AlphaFactory()

    def _get_engine(self) -> AltAlphaFactorEngine:
        """延迟初始化引擎。"""
        if self._engine is None:
            self._engine = AltAlphaFactorEngine()
        return self._engine

    def _get_cached(self, symbol: str) -> dict[str, Any] | None:
        """获取当日缓存，跨日自动失效。"""
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        cached_date, result = entry
        if cached_date != date.today():
            del self._cache[symbol]
            return None
        return result

    def _set_cached(self, symbol: str, result: dict[str, Any]) -> None:
        """写入当日缓存。"""
        self._cache[symbol] = (date.today(), result)

    async def check(self, symbol: str) -> dict[str, Any]:
        """检查个股另类因子评分。

        Args:
            symbol: 股票代码，如 ``"600519"``。

        Returns:
            包含 pass / altalpha_score / combined_score / top_factors /
            reasons / data_missing 的字典。
        """
        cached = self._get_cached(symbol)
        if cached is not None:
            return cached

        code = str(symbol).strip().zfill(6)
        thresholds = self._load_thresholds()

        # 数据缺失默认结果（fail-open）
        default: dict[str, Any] = {
            "pass": True,
            "altalpha_score": 0.0,
            "combined_score": 0.0,
            "tech_score": 0.0,
            "top_factors": [],
            "reasons": ["另类因子数据不可用"],
            "data_missing": True,
        }

        try:
            engine = self._get_engine()
            all_factors = await engine.compute_all(
                [code], fetch_financial=thresholds["fetch_financial"]
            )
            alt_data = engine.compute_single(code, all_factors)

            # 如果所有因子都是 0，认为数据缺失
            non_zero = [v for v in alt_data.values() if v != 0.0]
            if not non_zero:
                logger.debug("{} 另类因子全为 0，默认放行", code)
                self._set_cached(symbol, default)
                return default

            alt_factors = self._factory.compute_altalpha_factors(alt_data)
            result = self._factory.composite_score_with_altalpha(
                tech_factors={},
                alt_factors=alt_factors,
                altalpha_weight=thresholds["altalpha_weight"],
            )

            alt_score = result["altalpha_score"]
            combined_score = result["score"]
            top_factors = result.get("top_factors", [])

            reasons: list[str] = []
            confidence_boost = 0.0

            if combined_score < thresholds["block_threshold"]:
                reasons.append(
                    f"另类因子综合评分 {combined_score:.2f} < "
                    f"{thresholds['block_threshold']:.2f}，事件面偏弱"
                )
                passed = False
            elif combined_score > thresholds["boost_threshold"]:
                confidence_boost = thresholds["confidence_boost"]
                reasons.append(
                    f"另类因子综合评分 {combined_score:.2f} > "
                    f"{thresholds['boost_threshold']:.2f}，事件面支撑，"
                    f"confidence +{confidence_boost:.2f}"
                )
                passed = True
            else:
                reasons.append(f"另类因子综合评分 {combined_score:.2f}，事件面中性")
                passed = True

            output: dict[str, Any] = {
                "pass": passed,
                "altalpha_score": round(alt_score, 4),
                "combined_score": round(combined_score, 4),
                "tech_score": round(result.get("tech_score", 0.0), 4),
                "top_factors": top_factors,
                "confidence_boost": round(confidence_boost, 4),
                "reasons": reasons,
                "data_missing": False,
            }
            self._set_cached(symbol, output)
            return output

        except Exception as e:
            logger.warning("另类因子检查异常 {}: {}", code, e)
            self._set_cached(symbol, default)
            return default

    def _load_thresholds(self) -> dict[str, Any]:
        """从 settings.yaml 加载阈值，配置缺失时用默认值。"""
        defaults: dict[str, Any] = {
            "block_threshold": self.DEFAULT_BLOCK_THRESHOLD,
            "boost_threshold": self.DEFAULT_BOOST_THRESHOLD,
            "confidence_boost": self.DEFAULT_CONFIDENCE_BOOST,
            "altalpha_weight": self.DEFAULT_ALTPHA_WEIGHT,
            "fetch_financial": self.DEFAULT_FETCH_FINANCIAL,
        }
        try:
            from gugu._legacy_config import load_yaml

            raw = load_yaml("settings")
            section = raw.get("altalpha_filter", {})
            if not isinstance(section, dict):
                section = {}
        except Exception:
            section = {}

        for key in defaults:
            if key in section:
                try:
                    if key == "fetch_financial":
                        defaults[key] = bool(section[key])
                    else:
                        defaults[key] = float(section[key])
                except (ValueError, TypeError):
                    pass

        return defaults

    def describe(self) -> dict[str, Any]:
        """返回过滤器描述（用于飞书看板/调试）。"""
        return {
            "name": "另类 Alpha 因子过滤器",
            "block_threshold": self.DEFAULT_BLOCK_THRESHOLD,
            "boost_threshold": self.DEFAULT_BOOST_THRESHOLD,
            "confidence_boost": self.DEFAULT_CONFIDENCE_BOOST,
            "altalpha_weight": self.DEFAULT_ALTPHA_WEIGHT,
            "positioning": "信号增强器（事件驱动/行为金融因子，数据缺失默认放行）",
        }
