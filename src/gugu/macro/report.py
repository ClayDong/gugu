"""五维宏观日报报告生成器 — 构建飞书卡片消息。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gugu.macro.models import MacroSnapshot
from gugu.utils.log import get_logger

logger = get_logger()


def _fmt(val: float, suffix: str = "") -> str:
    return f"{val:.2f}{suffix}" if val else "--"


def _fmt_change(val: float) -> str:
    if val == 0:
        return "0.00%"
    icon = "🟢" if val > 0 else "🔴"
    return f"{icon} {abs(val):.2f}%"


class MacroReportBuilder:
    """构建宏观日报飞书卡片。"""

    def build_early_card(self, snapshot: MacroSnapshot) -> dict[str, Any]:
        """早间全球简报（08:00）：金油汇债G 五维概览。"""
        d = snapshot.to_dict()
        g = d["gold"]
        o = d["oil"]
        fx = d["fx"]
        b = d["bond"]
        dr = d["derivative"]

        lines = [
            "🌏 **全球隔夜简报**",
            f"⏰ {snapshot.timestamp[:16]}",
            "",
            "---",
            "",
            "**🥇 贵金属**",
            f"黄金: $ {_fmt(g['gold_price'])}  {_fmt_change(g['gold_change_pct'])}",
            f"白银: $ {_fmt(g['silver_price'])}  | 金银比: {g['gold_silver_ratio'] or '--'}",
            "",
            "**🛢️ 原油**",
            f"布伦特: $ {_fmt(o['brent_price'])}  {_fmt_change(o['brent_change_pct'])}",
            f"WTI: $ {_fmt(o['wti_price'])}  | 价差: $ {_fmt(o['spread'])}",
            "",
            "**💱 外汇**",
        ]
        if fx["valid"]:
            lines += [
                f"美元指数: {_fmt(fx['usd_index'])}  {_fmt_change(fx['usd_index_change_pct'])}",
                f"USD/CNY: {_fmt(fx['usd_cny'])}  | EUR/USD: {_fmt(fx['eur_usd'])}",
            ]
        else:
            lines.append("外汇数据暂不可用 ⚠️")

        lines += [
            "",
            "**📜 债券**",
            f"美债10Y: {_fmt(b['us10y_yield'])}%  | 2Y: {_fmt(b['us2y_yield'])}%",
            f"利差: {_fmt(b['us10y_2y_spread'])}% {'⚠️ 倒挂!' if b.get('inverted') else '✅ 正常'}",
            f"LPR: 1Y={_fmt(b['lpr_1y'])}%  5Y={_fmt(b['lpr_5y'])}%",
            "",
            "**🌐 衍生品**",
        ]
        if dr["valid"]:
            lines += [
                f"BDI: {_fmt(dr['bdi'])}  {_fmt_change(dr['bdi_change_pct'])}",
            ]
            if dr["vix"]:
                lines.append(f"VIX: {_fmt(dr['vix'])}")
            if dr["north_flow"]:
                lines.append(f"北向资金: {_fmt(dr['north_flow'])} 亿元")
        else:
            lines.append("衍生品数据暂不可用 ⚠️")

        # 置信度
        valid_count = sum([
            g["valid"], o["valid"], fx["valid"],
            b["valid"], dr["valid"],
        ])
        confidence = {5: "高", 4: "中", 3: "低"}.get(valid_count, "低")
        lines += [
            "",
            "---",
            f"📊 数据置信度: **{confidence}** ({valid_count}/5 维度有效)",
        ]

        return {
            "header": {
                "title": {"tag": "plain_text", "content": "🌏 全球隔夜简报"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ],
        }

    def build_morning_card(self, snapshot: MacroSnapshot) -> dict[str, Any]:
        """早盘简报（09:10）。"""
        card = self.build_early_card(snapshot)
        card["header"]["title"]["content"] = "☀️ A 股盘前简报"
        card["header"]["template"] = "indigo"
        return card

    def build_close_card(self, snapshot: MacroSnapshot) -> dict[str, Any]:
        """收盘简报（15:10）。"""
        card = self.build_early_card(snapshot)
        card["header"]["title"]["content"] = "🏁 收盘全球概览"
        card["header"]["template"] = "green"
        return card

    def build_text_summary(self, snapshot: MacroSnapshot) -> str:
        """构建纯文本摘要（用于 LLM prompt 上下文）。"""
        d = snapshot.to_dict()
        parts = [
            f"【宏观日报 {snapshot.timestamp[:10]}】",
        ]
        if d["gold"]["valid"]:
            parts.append(f"黄金 ${d['gold']['gold_price']}")
        if d["oil"]["valid"]:
            parts.append(f"布伦特 ${d['oil']['brent_price']} WTI ${d['oil']['wti_price']}")
        if d["bond"]["valid"]:
            parts.append(f"美债10Y {d['bond']['us10y_yield']}%")
        if d["derivative"]["valid"]:
            parts.append(f"BDI {d['derivative']['bdi']:.0f}")
        return " | ".join(parts) if parts else "宏观数据暂不可用"
