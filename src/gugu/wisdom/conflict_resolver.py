"""多视角冲突裁决器：多维权重融合算法。

核心理念（用户提出）：不同书籍对同一场景可能给出矛盾建议，
通过权重因子算法解决——场景适配度 × 历史胜率 × 用户画像 × 时效性，
加权融合得到可解释的最终决策。

流程：
1. gather_opinions(): 调用 LLM 一次性获取多视角独立建议
2. compute_weights(): 计算每个视角的多维权重
3. resolve(): 加权融合（action投票 + position加权平均 + stop_loss取最严格）
4. 检测冲突（action分散度 > 阈值）

冷启动：历史胜率权重刈1.0，累计≥5次后启用
学习：data/perspective_track.jsonl 记录建议结果，用于胜率更新
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

from gugu.config import PROJECT_ROOT, env
from gugu.models.user_profile import UserProfile, load_profile
from gugu.utils.log import get_logger
from gugu.wisdom.book_router import (
    BOOK_CATEGORIES,
    SCENARIO_MAPPING,
    BookPerspectiveRouter,
)

logger = get_logger()

# 历史追踪文件
TRACK_FILE = PROJECT_ROOT / "data" / "perspective_track.jsonl"

# 冷启动阈值：少于该次数时历史胜率权重=1.0
COLD_START_MIN = 5

# 冲突检测阈值：action 分散度 > 0.3 视为冲突
CONFLICT_DISPERSION_THRESHOLD = 0.3

# 类别 → 视角名 反查表（用于查 category）
_PERSPECTIVE_TO_CATEGORY: dict[str, str] = {}
for cat, perspectives in BOOK_CATEGORIES.items():
    for p in perspectives:
        _PERSPECTIVE_TO_CATEGORY[p] = cat


@dataclass
class PerspectiveOpinion:
    """单视角决策建议。"""

    perspective: str  # 视角目录名
    category: str  # 所属类别（value_investing 等）
    action: str  # buy | sell | hold | filter
    position_ratio: float  # 建议仓位 0.0-1.0
    stop_loss_price: float | None  # 止损价
    confidence: float  # 置信度 0.0-1.0
    reason: str  # 决策理由
    citation: str = ""  # 原文引用（可选）


@dataclass
class Resolution:
    """冲突裁决结果。"""

    final_action: str
    final_position_ratio: float
    final_stop_loss_price: float | None
    is_conflict: bool
    dispersion: float
    opinions: list[PerspectiveOpinion]
    weights: dict[str, float]  # perspective_name -> weight
    action_scores: dict[str, float]  # action -> weighted score
    dissenting_views: list[str]  # 分歧视角说明

    def to_dict(self) -> dict:
        return {
            "final_action": self.final_action,
            "final_position_ratio": self.final_position_ratio,
            "final_stop_loss_price": self.final_stop_loss_price,
            "is_conflict": self.is_conflict,
            "dispersion": round(self.dispersion, 3),
            "opinions": [
                {
                    "perspective": o.perspective,
                    "category": o.category,
                    "action": o.action,
                    "position_ratio": o.position_ratio,
                    "stop_loss_price": o.stop_loss_price,
                    "confidence": o.confidence,
                    "reason": o.reason,
                }
                for o in self.opinions
            ],
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
            "action_scores": {k: round(v, 3) for k, v in self.action_scores.items()},
            "dissenting_views": self.dissenting_views,
        }


class ConflictResolver:
    """多视角冲突裁决器。"""

    def __init__(self) -> None:
        self._router = BookPerspectiveRouter()
        self._async_client: httpx.AsyncClient | None = None
        cfg = env()
        self._llm_available = bool(cfg.llm_api_key and cfg.llm_base_url and cfg.llm_model)
        self._llm_cfg = cfg
        # 历史胜率缓存
        self._winrate_cache: dict[str, float | None] = {}

    async def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=30.0)
        return self._async_client

    async def close(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def resolve_conflict(
        self,
        scenario: str,
        signal: dict[str, Any],
        profile: UserProfile | None = None,
        direction: str = "",
    ) -> Resolution | None:
        """完整冲突裁决流程。

        Args:
            scenario: 交易场景 (entry/stop_loss/position_sizing/profit_taking 等)
            signal: 信号上下文
            profile: 用户画像
            direction: 信号方向 (buy/sell)

        Returns:
            Resolution 或 None（LLM 不可用或无视角时）
        """
        if profile is None:
            profile = load_profile()

        # 1. 选择视角
        atr = signal.get("atr", 0)
        price = signal.get("price", 0)
        market_vol = (atr / price) if (atr > 0 and price > 0) else None
        perspectives = self._router.select_perspectives_dynamic(
            scenario, direction, market_volatility=market_vol, signal=signal
        )
        if not perspectives:
            return None

        # 2. 获取多视角建议
        opinions = await self._gather_opinions(perspectives, scenario, signal, direction)
        if not opinions:
            return None

        # 3. 计算权重
        weights = self._compute_weights(opinions, scenario, profile, market_vol)

        # 4. 加权融合
        resolution = self._fuse(opinions, weights)

        logger.info(
            f"[conflict-resolver] 场景={scenario} 视角数={len(opinions)} "
            f"最终决策={resolution.final_action} 冲突={resolution.is_conflict} "
            f"分散度={resolution.dispersion:.2f}"
        )
        return resolution

    async def _gather_opinions(
        self,
        perspectives: list[str],
        scenario: str,
        signal: dict[str, Any],
        direction: str,
    ) -> list[PerspectiveOpinion]:
        """调用 LLM 获取多视角独立建议。"""
        if not self._llm_available:
            return self._fallback_opinions(perspectives, signal, direction)

        # 构建多视角上下文
        perspective_contexts = []
        for p in perspectives:
            content = self._router._perspectives.get(p, "")
            extracted = self._router._extract_core_wisdom(content, p)
            category = _PERSPECTIVE_TO_CATEGORY.get(p, "trading_system")
            perspective_contexts.append(
                f"## 视角: {p} (类别: {category})\n{extracted}"
            )
        perspectives_text = "\n\n".join(perspective_contexts)

        # 构建信号上下文
        signal_ctx = self._build_signal_context(signal)

        system_prompt = (
            "你是多视角交易决策系统。以下有多个交易大师的视角。\n"
            "请分别从每个大师的视角，对当前信号给出独立的决策建议。\n"
            "每个视角的建议必须忠于该大师的理念，不要融合其他视角。\n\n"
            "输出 JSON 数组，每个元素：\n"
            '{"perspective": "视角名", "action": "buy|sell|hold|filter", '
            '"position_ratio": 0.0-1.0, "stop_loss_price": 数值或null, '
            '"confidence": 0.0-1.0, "reason": "基于该视角的理由"}\n\n'
            "注意：position_ratio 是建议仓位占总资产比例，不超过 0.3。"
        )
        user_prompt = (
            f"## 信号方向: {direction}\n## 场景: {scenario}\n\n"
            f"{signal_ctx}\n\n## 多视角认知参考\n{perspectives_text}\n\n"
            f"请从以下视角分别给出建议: {perspectives}"
        )

        try:
            client = await self._get_async_client()
            resp = await client.post(
                f"{self._llm_cfg.llm_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._llm_cfg.llm_api_key}"},
                json={
                    "model": self._llm_cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 1200,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_opinions(content, perspectives)
        except Exception as e:
            logger.warning(f"[conflict-resolver] LLM 获取多视角建议失败，降级: {e}")
            return self._fallback_opinions(perspectives, signal, direction)

    def _parse_opinions(
        self, content: str, perspectives: list[str]
    ) -> list[PerspectiveOpinion]:
        """解析 LLM 输出的多视角建议。"""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            import re

            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    return []
            else:
                return []

        # LLM 可能返回 {opinions: [...]} 或 [...]
        if isinstance(data, dict):
            opinions_data = data.get("opinions", data.get("data", [data]))
        elif isinstance(data, list):
            opinions_data = data
        else:
            return []

        opinions: list[PerspectiveOpinion] = []
        for item in opinions_data:
            if not isinstance(item, dict):
                continue
            p = item.get("perspective", "")
            if p not in perspectives:
                continue
            action = item.get("action", "hold")
            if action not in ("buy", "sell", "hold", "filter"):
                action = "hold"
            opinions.append(
                PerspectiveOpinion(
                    perspective=p,
                    category=_PERSPECTIVE_TO_CATEGORY.get(p, "trading_system"),
                    action=action,
                    position_ratio=min(float(item.get("position_ratio", 0)), 0.3),
                    stop_loss_price=(
                        float(item["stop_loss_price"])
                        if item.get("stop_loss_price") is not None
                        else None
                    ),
                    confidence=min(max(float(item.get("confidence", 0.5)), 0.0), 1.0),
                    reason=item.get("reason", "")[:200],
                )
            )
        return opinions

    def _fallback_opinions(
        self, perspectives: list[str], signal: dict[str, Any], direction: str
    ) -> list[PerspectiveOpinion]:
        """LLM 不可用时的降级：每个视角给出与信号方向一致的建议。"""
        opinions: list[PerspectiveOpinion] = []
        action = "buy" if direction == "buy" else "sell" if direction == "sell" else "hold"
        for p in perspectives:
            opinions.append(
                PerspectiveOpinion(
                    perspective=p,
                    category=_PERSPECTIVE_TO_CATEGORY.get(p, "trading_system"),
                    action=action,
                    position_ratio=0.1,  # 保守降级
                    stop_loss_price=None,
                    confidence=0.5,
                    reason="LLM 不可用，降级为与信号方向一致",
                )
            )
        return opinions

    def _build_signal_context(self, signal: dict[str, Any]) -> str:
        """构建信号上下文。"""
        direction = signal.get("direction", "")
        lines = [
            f"- 股票: {signal.get('symbol', '')} {signal.get('name', '')}",
            f"- 方向: {direction}",
            f"- 策略: {signal.get('strategy', '')}",
            f"- 理由: {signal.get('reason', '')}",
            f"- 当前价: {signal.get('price', 0):.2f}",
            f"- 置信度: {signal.get('confidence', 0):.2f}",
            f"- 建议仓位: {signal.get('suggested_position_ratio', 0):.2%}",
        ]
        if signal.get("has_position"):
            lines.append(f"- 已有持仓占比: {signal.get('current_position_ratio', 0):.2%}")
        return "\n".join(lines)

    # ========== 权重计算 ==========

    def _compute_weights(
        self,
        opinions: list[PerspectiveOpinion],
        scenario: str,
        profile: UserProfile,
        market_vol: float | None,
    ) -> dict[str, float]:
        """多维权重计算：场景适配 × 历史胜率 × 用户画像 × 时效性。"""
        weights: dict[str, float] = {}
        for op in opinions:
            w_scenario = self._scenario_fitness(op.category, scenario)
            w_winrate = self._historical_winrate(op.perspective) or 1.0
            w_profile = profile.get_style_weight(op.category)
            w_temporal = self._temporal_fitness(op.category, market_vol)
            # 乘法融合：任一维度低则整体低
            raw = w_scenario * w_winrate * w_profile * w_temporal
            weights[op.perspective] = max(raw, 0.01)  # 避免完全为零

        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}
        return weights

    def _scenario_fitness(self, category: str, scenario: str) -> float:
        """维度1：场景适配度。SCENARIO_MAPPING 中第1类=1.0，第2类=0.8，第3类=0.6。"""
        categories = SCENARIO_MAPPING.get(scenario, ["trading_system"])
        if category in categories:
            idx = categories.index(category)
            return max(1.0 - idx * 0.2, 0.4)
        return 0.5  # 不在映射中

    def _historical_winrate(self, perspective: str) -> float | None:
        """维度2：历史胜率。冷启动（<5次）返回 None（视为1.0）。"""
        if perspective in self._winrate_cache:
            return self._winrate_cache[perspective]

        if not TRACK_FILE.exists():
            self._winrate_cache[perspective] = None
            return None

        records: list[dict] = []
        try:
            with TRACK_FILE.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            self._winrate_cache[perspective] = None
            return None

        # 过滤该视角的记录
        perspective_records = [r for r in records if r.get("perspective") == perspective]
        if len(perspective_records) < COLD_START_MIN:
            self._winrate_cache[perspective] = None
            return None

        # 胜率 = action 与最终结果一致的比例
        wins = sum(1 for r in perspective_records if r.get("correct", False))
        winrate = wins / len(perspective_records)
        # 映射到 0.5-1.5 区间（50%胜率=1.0，100%胜率=1.5，0%胜率=0.5）
        weighted = 0.5 + winrate
        self._winrate_cache[perspective] = weighted
        return weighted

    def _temporal_fitness(self, category: str, market_vol: float | None) -> float:
        """维度4：时效性。高波动→技术分析/交易系统↑，低波动→价值投资/宏观↑。"""
        if market_vol is None:
            return 1.0
        if market_vol > 0.03:  # 高波动
            if category in ("technical_analysis", "trading_system"):
                return 1.2
            elif category in ("value_investing", "macro_economics"):
                return 0.8
        elif market_vol < 0.01:  # 低波动
            if category in ("value_investing", "macro_economics"):
                return 1.2
            elif category in ("technical_analysis",):
                return 0.8
        return 1.0

    # ========== 加权融合 ==========

    def _fuse(
        self, opinions: list[PerspectiveOpinion], weights: dict[str, float]
    ) -> Resolution:
        """加权融合裁决。"""
        if not opinions:
            return Resolution(
                final_action="hold",
                final_position_ratio=0.0,
                final_stop_loss_price=None,
                is_conflict=False,
                dispersion=0.0,
                opinions=[],
                weights={},
                action_scores={},
                dissenting_views=[],
            )

        # action 投票：加权多数
        action_scores: dict[str, float] = defaultdict(float)
        for op in opinions:
            w = weights.get(op.perspective, 1.0 / len(opinions))
            action_scores[op.action] += w
        total_weight = sum(action_scores.values())
        final_action = max(action_scores, key=action_scores.get)

        # position：加权平均
        final_position = 0.0
        for op in opinions:
            w = weights.get(op.perspective, 1.0 / len(opinions))
            final_position += op.position_ratio * w
        final_position = min(final_position, 0.3)  # 单股上限

        # stop_loss：取最严格（最高价，最接近现价，触发最早、亏损最小）
        stop_losses = [op.stop_loss_price for op in opinions if op.stop_loss_price and op.stop_loss_price > 0]
        final_stop = max(stop_losses) if stop_losses else None

        # 冲突检测：action 分散度 = 1 - max(score)/total
        max_score = max(action_scores.values())
        dispersion = 1.0 - (max_score / total_weight) if total_weight > 0 else 0.0
        is_conflict = dispersion > CONFLICT_DISPERSION_THRESHOLD

        # 分歧视角：与最终 action 不同的视角
        dissenting_views = []
        for op in opinions:
            if op.action != final_action:
                dissenting_views.append(
                    f"{op.perspective} 建议 {op.action}: {op.reason[:60]}"
                )

        return Resolution(
            final_action=final_action,
            final_position_ratio=round(final_position, 4),
            final_stop_loss_price=round(final_stop, 2) if final_stop else None,
            is_conflict=is_conflict,
            dispersion=dispersion,
            opinions=opinions,
            weights=weights,
            action_scores=dict(action_scores),
            dissenting_views=dissenting_views,
        )


# ========== 历史追踪记录 ==========


def record_opinion(
    perspective: str,
    action: str,
    signal_symbol: str,
    final_result: bool,
    scenario: str = "",
) -> None:
    """记录视角建议结果，用于历史胜率计算。

    Args:
        perspective: 视角名
        action: 建议的 action
        signal_symbol: 股票代码
        final_result: 该建议是否正确（True=盈利/正确，False=亏损/错误）
        scenario: 交易场景
    """
    import time

    TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "perspective": perspective,
        "action": action,
        "symbol": signal_symbol,
        "scenario": scenario,
        "correct": final_result,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with TRACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# 模块级单例
_resolver: ConflictResolver | None = None


def get_conflict_resolver() -> ConflictResolver:
    """获取冲突裁决器单例。"""
    global _resolver
    if _resolver is None:
        _resolver = ConflictResolver()
    return _resolver
