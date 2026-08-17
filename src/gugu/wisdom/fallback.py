"""硬编码 fallback 决策逻辑（LLM 不可用时使用）。

包含：
- 分层仓位管理（试仓/加仓/满仓）
- 止损/止盈计算
- 历史胜率注入
- 数据驱动建议生成
"""

from __future__ import annotations

import json
from typing import Any

import gugu.config as _gugu_config
from gugu.config import get_config
from gugu.utils.log import get_logger

logger = get_logger()

# ========== 硬编码 fallback 常量（LLM 不可用时使用） ==========
# 来源：stock-position-sizing / stock-stop-loss-decision / stock-entry-decision

FALLBACK_TRIAL_RATIO = 0.20  # 试仓比例 20%
FALLBACK_ADD_RATIO = 0.40  # 加码比例 40%
FALLBACK_STOP_LOSS_PCT = 0.08  # 默认止损比例 8%
FALLBACK_ENTRY_MIN_CONFIDENCE = 0.4  # 最低入场置信度
FALLBACK_TAKE_PROFIT_T1 = 0.10  # 第一止盈目标 +10%
FALLBACK_TAKE_PROFIT_T2 = 0.20  # 第二止盈目标 +20%


def _max_single_position() -> float:
    """从配置读取单股最大仓位上限，与风控 L1 保持一致。"""
    return get_config().risk.max_position_ratio


def _compute_take_profit(entry_price: float, atr: float = 0) -> dict:
    """计算止盈方案：第一目标 +10%、第二目标 +20%。

    若有 ATR 数据，用 ATR 校准止盈幅度；
    若无 ATR 则用固定百分比 fallback。

    Returns:
        {"target_1": float, "target_1_pct": float,
         "target_2": float, "target_2_pct": float,
         "reduce_at_target1": str, "note": str}
    """
    tp1_pct = FALLBACK_TAKE_PROFIT_T1
    tp2_pct = FALLBACK_TAKE_PROFIT_T2

    # ATR 校准：若 ATR/price > 3%，目标上调；若 < 1%，目标下调
    if atr > 0 and entry_price > 0:
        atr_ratio = atr / entry_price
        if atr_ratio > 0.03:
            tp1_pct = min(0.15, tp1_pct * 1.5)
            tp2_pct = min(0.30, tp2_pct * 1.5)
        elif atr_ratio < 0.01:
            tp1_pct = max(0.05, tp1_pct * 0.5)
            tp2_pct = max(0.10, tp2_pct * 0.5)

    return {
        "target_1": round(entry_price * (1 + tp1_pct), 2),
        "target_1_pct": tp1_pct,
        "target_2": round(entry_price * (1 + tp2_pct), 2),
        "target_2_pct": tp2_pct,
        "reduce_at_target1": "50%",
        "note": f"止损 {FALLBACK_STOP_LOSS_PCT:.0%} / 止盈 {tp1_pct:.0%}-{tp2_pct:.0%}，盈亏比 1:{tp2_pct / FALLBACK_STOP_LOSS_PCT:.1f}",
    }


def _is_profitable_signal(signal_record: dict) -> bool:
    """仅依据已平仓的实际正盈亏判断信号是否盈利。"""
    if signal_record.get("outcome_status") != "closed":
        return False

    realized_pnl = signal_record.get("realized_pnl")
    if realized_pnl is None:
        metrics = signal_record.get("outcome_metrics") or {}
        realized_pnl = metrics.get("realized_pnl")
    try:
        return float(realized_pnl) > 0
    except (TypeError, ValueError):
        return False


def get_strategy_win_rate(strategy_name: str) -> float | None:
    """获取策略的历史胜率（从 signals_history.jsonl 计算）。

    胜率定义：该策略发出的信号中，5 日后收益 > 0 的比例。
    若历史记录不足（< 5 条），返回 None（表示数据不足，不调整）。
    """
    path = _gugu_config.PROJECT_ROOT / "data" / "signals_history.jsonl"
    if not path.exists():
        return None

    # 读取最近 500 条信号
    records: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 500:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    records.append(rec)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return None

    if not records:
        return None

    # 仅使用已平仓记录
    strategy_signals = []
    for r in records:
        strategies = r.get("strategies", []) or [r.get("strategy", "")]
        if isinstance(strategies, str):
            strategies = [strategies]
        if strategy_name in strategies and r.get("outcome_status") == "closed":
            strategy_signals.append(r)

    if len(strategy_signals) < 3:
        return None  # 数据不足

    wins = sum(1 for r in strategy_signals if _is_profitable_signal(r))
    return wins / len(strategy_signals)


def get_skill_advice(skills: dict[str, str], skill_name: str) -> str:
    """获取指定 skill 的核心建议（截取关键段落，用于 fallback 展示）。"""
    content = skills.get(skill_name, "")
    if not content:
        return ""

    lines = content.split("\n")
    summary_lines = []
    for line in lines:
        if line.startswith("#") or line.startswith("- ") or line.startswith("  "):
            summary_lines.append(line)
        if len("\n".join(summary_lines)) > 500:
            break
    return "\n".join(summary_lines)


def get_skill_source(skills: dict[str, str], skill_name: str) -> str:
    """从 SKILL.md 的 YAML frontmatter 提取 source_book 字段。"""
    content = skills.get(skill_name, "")
    if not content:
        return ""

    # 解析 YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            frontmatter = content[3:end]
            for line in frontmatter.split("\n"):
                if line.strip().startswith("source_book:"):
                    return line.split(":", 1)[1].strip()
    return ""


def build_data_driven_advice(
    skills: dict[str, str],
    signal: dict[str, Any],
    direction: str,
) -> dict[str, str]:
    """基于信号实际数据生成交易智慧建议（替代格言摘录）。

    每条建议包含：
    1. 基于当前信号数据的具体结论
    2. 应用的策略原则（含出处）
    """
    advice: dict[str, str] = {}
    symbol = signal.get("symbol", "")
    name = signal.get("name", "") or symbol
    price = signal.get("price", 0.0)
    confidence = signal.get("confidence", 1.0)
    atr = signal.get("atr", 0.0)
    has_position = signal.get("has_position", False)
    profit_pct = signal.get("profit_pct", 0.0)
    strategies = signal.get("strategies", []) or [signal.get("strategy", "")]
    if isinstance(strategies, str):
        strategies = [strategies]

    # 市场分析数据（从 signal_pipeline 注入）
    stage = signal.get("stage", "")
    if isinstance(stage, dict):
        # signal_pipeline:486 注入的是 dict（stage/confidence/description/suggestion），
        # 本函数只消费阶段名（stage_map.get / in 元组判断），dict 不可哈希会抛
        # TypeError: unhashable type: 'dict'（2026-08-10 实测：首个真实信号必现，
        # 导致 signal_only Wisdom fallback 整体失败、智慧建议缺失）。取内层 stage 名。
        stage = (stage.get("stage") or "").strip() or ""
    danger_signals = signal.get("danger_signals", [])
    if isinstance(danger_signals, dict):
        # signal_pipeline:501 注入的是 dict（signals/severity/description/action），
        # 空 dict 也是 truthy，且 dict[:2] 抛 KeyError: slice(None, 2, None
        # （2026-08-10 实测：与 stage bug 同链路的第二处崩溃，无危险信号也必现）。
        # 取内层 signals 列表，空列表自然跳过提示。
        danger_signals = danger_signals.get("signals", []) or []
    divergence = signal.get("divergence", {})
    money_flow = signal.get("money_flow", {})
    fundamental = signal.get("fundamental", {})

    if direction == "buy":
        # ── 入场建议 ──
        entry_parts: list[str] = []
        entry_parts.append(f"**{name}({symbol})** 当前价 ¥{price:.2f}，置信度 {confidence:.0%}")

        # 阶段判断
        if stage:
            stage_map = {
                "bullish": "升势",
                "crazy": "疯狂",
                "final": "最后",
                "flat": "牛皮市",
            }
            stage_text = stage_map.get(stage, stage)
            entry_parts.append(f"市场阶段：{stage_text}")
            if stage in ("crazy", "final"):
                entry_parts.append("⚠️ 疯狂/最后阶段禁止入场（P0 规则）")

        # 危险信号
        if danger_signals:
            entry_parts.append(
                f"⚠️ 检测到 {len(danger_signals)} 个危险信号：{', '.join(danger_signals[:2])}"
            )

        # 量价背离
        if divergence and isinstance(divergence, dict) and divergence.get("diverged"):
            entry_parts.append("⚠️ 量价背离：价涨量缩，警惕主力出货")

        # 资金流
        if money_flow and isinstance(money_flow, dict):
            net_inflow = money_flow.get("net_inflow", 0)
            if net_inflow < 0:
                entry_parts.append(f"⚠️ 资金净流出 {abs(net_inflow):.2f}亿，主力在撤")

        # 基本面
        if fundamental and isinstance(fundamental, dict):
            pe = fundamental.get("pe")
            if pe and pe > 0:
                entry_parts.append(f"PE={pe:.1f}")

        # 策略出处
        source = get_skill_source(skills, "stock-entry-decision")
        entry_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["entry_check"] = "\n".join(entry_parts)

        # ── 止损建议 ──
        stop_parts: list[str] = []
        stop_price = signal.get("stop_loss_price", 0.0)
        if stop_price and price > 0:
            stop_pct = (price - stop_price) / price
            stop_parts.append(f"建议止损价：¥{stop_price:.2f}（-{stop_pct:.1%}）")
            if atr > 0:
                stop_parts.append(f"ATR={atr:.2f}（占价 {atr / price:.1%}），止损幅度参考 ATR")
            else:
                stop_parts.append("无 ATR 数据，使用默认 8% 止损")
        else:
            stop_parts.append(
                f"建议止损价：¥{price * 0.92:.2f}（-8%）" if price > 0 else "价格无效"
            )
        stop_parts.append("原则：止损是铁律，不可违反，跌破立即执行")
        source = get_skill_source(skills, "stock-stop-loss-decision")
        stop_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["stop_loss"] = "\n".join(stop_parts)

        # ── 仓位建议 ──
        pos_parts: list[str] = []
        ratio = signal.get("suggested_position_ratio", 0.0)
        if has_position:
            pos_parts.append(f"已持仓，当前盈利 {profit_pct:+.1%}")
            if profit_pct > 0.05:
                pos_parts.append(f"盈利确认，可加码至 {ratio:.1%}（加仓策略）")
            else:
                pos_parts.append(f"建议加码比例：{ratio:.1%}")
        else:
            pos_parts.append(f"未持仓，建议试仓 {ratio:.1%}（首次建仓 20%）")
            pos_parts.append("分层下注：试仓 20% → 盈利确认加码 40% → 满仓 30%")
        source = get_skill_source(skills, "stock-position-sizing")
        pos_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["position_sizing"] = "\n".join(pos_parts)

    elif direction == "sell":
        # ── 止盈建议 ──
        profit_parts: list[str] = []
        profit_parts.append(f"**{name}({symbol})** 卖出信号触发")
        profit_parts.append(f"当前价 ¥{price:.2f}，持仓盈利 {profit_pct:+.1%}")
        if profit_pct > 0.30:
            profit_parts.append("⚠️ 已达 +30%，按财叔策略应减仓 1/3")
        elif profit_pct > 0.15:
            profit_parts.append("💡 盈利 >15%，考虑部分止盈")
        elif profit_pct < -0.08:
            profit_parts.append("⚠️ 亏损 >8%，应执行止损")
        source = get_skill_source(skills, "stock-profit-taking-decision")
        profit_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["profit_taking"] = "\n".join(profit_parts)

        # ── 追踪止损 ──
        trail_parts: list[str] = []
        if has_position and profit_pct > 0:
            trail_parts.append(f"持仓盈利 {profit_pct:+.1%}，启用追踪止损")
            trail_parts.append("止损价随浪谷上移，只上不下")
        else:
            trail_parts.append("无持仓或亏损中，不适用追踪止损")
        source = get_skill_source(skills, "stock-trailing-stop")
        trail_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
        advice["trailing_stop"] = "\n".join(trail_parts)

    # ── 心态检查 ──
    psych_parts: list[str] = []
    if direction == "buy":
        if confidence < 0.6:
            psych_parts.append("⚠️ 置信度较低，警惕 FOMO 情绪驱动入场")
        if stage in ("crazy", "final"):
            psych_parts.append("⚠️ 市场过热，警惕贪婪情绪")
    elif direction == "sell":
        if profit_pct > 0.30:
            psych_parts.append("💡 盈利丰厚，警惕贪婪不愿卖")
        elif profit_pct < -0.05:
            psych_parts.append("💡 亏损中，警惕侥幸心理不止损")
    psych_parts.append("原则：纪律 > 情绪，按策略执行而非凭感觉")
    source = get_skill_source(skills, "stock-psychology-check")
    psych_parts.append(f"📍 策略出处：{source}" if source else "📍 策略出处：《炒股的智慧》")
    advice["psychology_check"] = "\n".join(psych_parts)

    return advice


def advise_fallback(
    skills: dict[str, str],
    signal: dict[str, Any],
    get_strategy_win_rate_fn=None,
) -> dict[str, Any]:
    """硬编码规则 fallback（LLM 不可用时使用）。

    历史胜率注入：若该策略历史胜率 < 30%，降低入场置信度阈值。
    """
    signal = dict(signal)
    direction = signal.get("direction", "")
    price = signal.get("price", 0.0)
    confidence = signal.get("confidence", 1.0)

    # 历史胜率注入
    strategies = signal.get("strategies", []) or [signal.get("strategy", "")]
    if isinstance(strategies, str):
        strategies = [strategies]
    for s_name in strategies:
        if s_name:
            hist_win_rate = (
                get_strategy_win_rate(s_name)
                if get_strategy_win_rate_fn is None
                else get_strategy_win_rate_fn(s_name)
            )
            if hist_win_rate is not None and hist_win_rate < 0.30:
                logger.info(
                    "[wisdom-fallback] 策略 {} 历史胜率 {:.1%} < 30%，降低入场置信度阈值（0.6 → 0.4）",
                    s_name,
                    hist_win_rate,
                )
                min_conf = 0.4
                if confidence < min_conf:
                    logger.info(
                        "[wisdom-fallback] 策略 {} 低胜率 {:.1%}，置信度 {:.2f} < {}，过滤信号",
                        s_name,
                        hist_win_rate,
                        confidence,
                        min_conf,
                    )
                    signal["wisdom_filtered"] = True
                    signal["filter_reason"] = (
                        f"策略 {s_name} 历史胜率 {hist_win_rate:.1%} 过低，信号被过滤"
                    )
                    return signal

    advice: dict[str, str] = {}
    decision: dict[str, Any] = {"source": "fallback"}

    if direction == "buy":
        # 数据驱动建议
        advice = build_data_driven_advice(skills, signal, "buy")

        # 入场过滤
        if confidence <= FALLBACK_ENTRY_MIN_CONFIDENCE:
            decision["entry_filtered"] = True
            decision["filter_reason"] = (
                f"置信度 {confidence:.2f} 低于入场阈值 {FALLBACK_ENTRY_MIN_CONFIDENCE}"
            )
            signal["wisdom_filtered"] = True
            logger.info(
                "[wisdom-fallback] {} 入场过滤: 置信度 {:.2f} < {}",
                signal.get("symbol", ""),
                confidence,
                FALLBACK_ENTRY_MIN_CONFIDENCE,
            )

        # 仓位调整
        original_ratio = signal.get("suggested_position_ratio", 0.0)
        is_signal_only = signal.get("_exec_mode") == "signal_only"
        if original_ratio <= 0 and is_signal_only:
            original_ratio = FALLBACK_TRIAL_RATIO
            decision["position_note"] = "当前市场状态仓位建议为0%，此为参考性试仓比例"
        if original_ratio > 0:
            has_position = signal.get("has_position", False)
            profit_pct = signal.get("profit_pct", 0.0)
            if has_position:
                if profit_pct > 0.05:
                    adjusted_ratio = original_ratio * FALLBACK_ADD_RATIO * 1.5
                    strategy_label = "add_confirmed"
                else:
                    adjusted_ratio = original_ratio * FALLBACK_ADD_RATIO
                    strategy_label = "add"
            else:
                adjusted_ratio = original_ratio * FALLBACK_TRIAL_RATIO
                strategy_label = "trial"
            current_ratio = signal.get("current_position_ratio", 0.0)
            max_ratio = _max_single_position()
            remaining = max_ratio - float(current_ratio)
            if remaining <= 0 and not is_signal_only:
                adjusted_ratio = 0.0
                decision["filter_reason"] = (
                    f"现有持仓占比 {current_ratio:.2%} 已达上限 {max_ratio:.2%}，不再加仓"
                )
                signal["wisdom_filtered"] = True
            else:
                if is_signal_only:
                    adjusted_ratio = min(adjusted_ratio, max_ratio)
                else:
                    adjusted_ratio = min(adjusted_ratio, remaining)
            decision["adjusted_position_ratio"] = adjusted_ratio
            decision["position_strategy"] = strategy_label
            signal["suggested_position_ratio"] = adjusted_ratio
            logger.info(
                "[wisdom-fallback] {} 仓位调整: {:.2%} → {:.2%} ({})",
                signal.get("symbol", ""),
                original_ratio,
                adjusted_ratio,
                strategy_label,
            )

        # 止损价预设
        if price > 0:
            stop_price = price * (1 - FALLBACK_STOP_LOSS_PCT)
            decision["stop_loss_price"] = round(stop_price, 2)
            signal["stop_loss_price"] = round(stop_price, 2)
            # 止盈方案
            tp = _compute_take_profit(price, signal.get("atr", 0))
            signal["take_profit"] = tp
            decision["take_profit"] = tp
            logger.info(
                "[wisdom-fallback] {} 止损预设: {:.2f} (-{:.0%}) | 止盈目标: +{:.0%}/+{:.0%}",
                signal.get("symbol", ""),
                stop_price,
                FALLBACK_STOP_LOSS_PCT,
                tp.get("target_1_pct", 0),
                tp.get("target_2_pct", 0),
            )

    elif direction == "sell":
        # 数据驱动建议
        advice = build_data_driven_advice(skills, signal, "sell")

        # 无持仓时过滤卖出信号
        has_position = signal.get("has_position", False)
        if not has_position:
            signal["wisdom_filtered"] = True
            decision["filter_reason"] = "无持仓，过滤卖出信号"
            logger.info("[wisdom-fallback] {} 卖出过滤: 无持仓", signal.get("symbol", ""))
        elif price > 0:
            decision["sell_reason"] = "策略信号触发卖出"
            exit_price = round(price, 2)
            decision["stop_loss_price"] = exit_price
            signal["stop_loss_price"] = exit_price
            logger.info(
                "[wisdom-fallback] {} 卖出决策: 策略信号触发, 退出价: {:.2f}",
                signal.get("symbol", ""),
                exit_price,
            )

    signal["wisdom"] = advice
    signal["wisdom_decision"] = decision

    return signal
