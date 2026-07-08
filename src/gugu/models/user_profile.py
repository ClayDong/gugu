"""用户画像：个性化投资指导的基础设施。

存储用户的风险偏好、投资风格、经验等级和投资期限，
为冲突裁决器提供视角权重、为小白学堂提供概念推送顺序、
为 KB Agent 提供答案深度调节。

存储路径：data/user_profile.json
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

_PROFILE_FILE = PROJECT_ROOT / "data" / "user_profile.json"

# 6大类视角（与 book_router.BOOK_CATEGORIES 对齐）
PERSPECTIVE_CATEGORIES = (
    "value_investing",
    "growth_selecting",
    "technical_analysis",
    "trading_system",
    "macro_economics",
    "investment_philosophy",
)

# 风格 × 期限 → 视角偏好权重矩阵
# 对抗性审查：基于投资期限与风格的客观匹配，非主观偏好
# 价值型+长期 → 价值投资权重高，交易系统权重低
# 趋势型+短期 → 交易系统权重高，价值投资权重低
_STYLE_WEIGHTS: dict[str, dict[str, float]] = {
    "value": {
        "value_investing": 1.3,
        "growth_selecting": 1.0,
        "technical_analysis": 0.8,
        "trading_system": 0.7,
        "macro_economics": 1.0,
        "investment_philosophy": 1.1,
    },
    "growth": {
        "value_investing": 0.9,
        "growth_selecting": 1.2,
        "technical_analysis": 1.0,
        "trading_system": 0.9,
        "macro_economics": 1.0,
        "investment_philosophy": 1.0,
    },
    "trend": {
        "value_investing": 0.7,
        "growth_selecting": 0.9,
        "technical_analysis": 1.3,
        "trading_system": 1.3,
        "macro_economics": 0.9,
        "investment_philosophy": 1.0,
    },
    "mixed": {
        "value_investing": 1.0,
        "growth_selecting": 1.0,
        "technical_analysis": 1.0,
        "trading_system": 1.0,
        "macro_economics": 1.0,
        "investment_philosophy": 1.0,
    },
}

# 默认画像：平衡型 + 混合风格 + 新手 + 中期
_DEFAULT_PROFILE = {
    "risk_tolerance": "balanced",
    "investment_style": "mixed",
    "experience": "novice",
    "horizon": "medium",
    "style_weights": _STYLE_WEIGHTS["mixed"],
}


@dataclass
class UserProfile:
    """用户画像数据类。"""

    risk_tolerance: str = "balanced"  # conservative | balanced | aggressive
    investment_style: str = "mixed"  # value | growth | trend | mixed
    experience: str = "novice"  # novice | intermediate | expert
    horizon: str = "medium"  # short | medium | long
    style_weights: dict[str, float] = field(default_factory=lambda: dict(_STYLE_WEIGHTS["mixed"]))

    def get_style_weight(self, category: str) -> float:
        """获取指定视角类别的风格权重。"""
        return self.style_weights.get(category, 1.0)

    def is_novice(self) -> bool:
        """是否为新手用户（影响 KB 答案深度）。"""
        return self.experience == "novice"

    def to_dict(self) -> dict:
        return asdict(self)


def load_profile() -> UserProfile:
    """加载用户画像，不存在则返回默认画像并持久化。"""
    if not _PROFILE_FILE.exists():
        logger.info(f"[user-profile] 未找到画像文件，创建默认画像: {_PROFILE_FILE}")
        profile = UserProfile()
        save_profile(profile)
        return profile

    try:
        data = json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))
        # 兼容旧数据：若 style_weights 缺失，按 investment_style 重建
        if "style_weights" not in data or not data["style_weights"]:
            data["style_weights"] = _STYLE_WEIGHTS.get(
                data.get("investment_style", "mixed"), _STYLE_WEIGHTS["mixed"]
            )
        return UserProfile(**data)
    except Exception as e:
        logger.warning(f"[user-profile] 加载失败 ({e})，使用默认画像")
        return UserProfile()


def save_profile(profile: UserProfile) -> None:
    """持久化用户画像。"""
    _PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROFILE_FILE.write_text(
        json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        f"[user-profile] 已保存: style={profile.investment_style}, "
        f"risk={profile.risk_tolerance}, exp={profile.experience}"
    )


def get_style_weight(category: str, profile: UserProfile | None = None) -> float:
    """便捷函数：获取视角类别的风格权重。"""
    if profile is None:
        profile = load_profile()
    return profile.get_style_weight(category)


def rebuild_style_weights(style: str) -> dict[str, float]:
    """根据投资风格重建权重表（用户修改 style 时调用）。"""
    return dict(_STYLE_WEIGHTS.get(style, _STYLE_WEIGHTS["mixed"]))
