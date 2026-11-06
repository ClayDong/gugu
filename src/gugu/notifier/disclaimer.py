"""对外呈现的统一免责声明（单一事实源）。

2026-11-04 评审 P0-4：此前免责声明只在 signal_only 分支出现，LLM 解读
时消失；Web 展示收益/胜率却无免责；实验信号被包装成「买入信号」直推业务群。
本模块把所有对外文本的免责与样本量门槛收敛到一处，便于契约测试锁定。
"""

from __future__ import annotations

DISCLAIMER_TEXT = (
    "⚠️ 仅供研究参考，不构成投资建议。信号由量化模型自动生成，"
    "历史表现与模拟盘数据不代表未来收益；请自行判断并承担风险。"
)

# 少于该样本数时禁止展示胜率 / 收益统计，避免小样本误导
MIN_PERFORMANCE_SAMPLES = 30


def disclaimer() -> str:
    """返回统一免责文案。"""
    return DISCLAIMER_TEXT


def with_disclaimer(text: str) -> str:
    """幂等地把免责声明追加到任意对外文本末尾。"""
    if DISCLAIMER_TEXT in text:
        return text
    if not text.strip():
        return DISCLAIMER_TEXT
    return f"{text.rstrip()}\n\n---\n{DISCLAIMER_TEXT}"


def performance_note(sample_count: int) -> str | None:
    """样本不足时返回提示文案（调用方应据此隐藏胜率/收益数字）。"""
    if sample_count < MIN_PERFORMANCE_SAMPLES:
        return f"样本不足（n={sample_count} < {MIN_PERFORMANCE_SAMPLES}），暂不展示胜率与收益统计"
    return None


def resolve_win_rate(perf: dict) -> float:
    """兼容多个上游字段名，修复 formatter 恒显示「胜率 0%」的问题。

    engine_mixin 注入的是 forward_5d_signal_hit_rate，而旧代码只读 win_rate。
    """
    for key in ("win_rate", "forward_5d_signal_hit_rate", "hit_rate"):
        value = perf.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0
