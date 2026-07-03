"""五维宏观日报报告生成器 — 构建飞书卡片消息。"""

from __future__ import annotations

from typing import Any

from gugu.macro.models import MacroSnapshot
from gugu.utils.log import get_logger

logger = get_logger()


def _fmt(val: float, suffix: str = "") -> str:
    return f"{val:.2f}{suffix}" if val else "--"


def _fmt_change(val: float) -> str:
    """格式化涨跌幅（A 股惯例：涨红跌绿，带正负号）。"""
    if val == 0:
        return "0.00%"
    icon = "🔴" if val > 0 else "🟢"
    return f"{icon} {val:+.2f}%"


class MacroReportBuilder:
    """构建宏观日报飞书卡片。"""

    def build_early_card(self, snapshot: MacroSnapshot) -> dict[str, Any]:
        """早间全球简报（08:00）：A 股大盘 + 金油汇债G 六维概览。"""
        d = snapshot.to_dict()
        g = d["gold"]
        o = d["oil"]
        fx = d["fx"]
        b = d["bond"]
        dr = d["derivative"]
        si = d["stock_index"]

        lines = [
            "🌏 **全球隔夜简报**",
            f"⏰ {snapshot.timestamp[:16]}",
            "",
        ]

        # ── A 股大盘指数（用户最关注，放在最前面）──
        if si["valid"]:
            lines += [
                "---",
                "",
                "**📈 A 股大盘指数**",
            ]
            # 仅显示有效指数（price > 0），避免采集失败的指数显示 0.00
            if si.get("sh_index", 0) > 0:
                lines.append(f"上证指数: {si['sh_index']:.2f}  {_fmt_change(si['sh_change_pct'])}")
            if si.get("sz_index", 0) > 0:
                lines.append(f"深证成指: {si['sz_index']:.2f}  {_fmt_change(si['sz_change_pct'])}")
            if si.get("cyb_index", 0) > 0:
                lines.append(f"创业板指: {si['cyb_index']:.2f}  {_fmt_change(si['cyb_change_pct'])}")
            if si.get("total_volume", 0) > 0:
                lines.append(f"沪深成交额: {si['total_volume']:.0f} 亿")
            if si.get("limit_up_count", 0) > 0 or si.get("limit_down_count", 0) > 0:
                lines.append(
                    f"涨停 {si['limit_up_count']} 家 | 跌停 {si['limit_down_count']} 家"
                )
            # 北向资金（None 或 0 均显示"获取不到"，避免误导）
            nf = dr.get("north_flow")
            if nf is not None and nf != 0:
                nf_str = f"🔴 +{nf:.1f} 亿" if nf > 0 else f"🟢 {nf:.1f} 亿"
                lines.append(f"北向资金: {nf_str}")
            else:
                lines.append("北向资金: 获取不到")
            lines += [""]

        lines += [
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
        else:
            lines.append("衍生品数据暂不可用 ⚠️")

        # 置信度（含 A 股指数维度）
        valid_count = sum(
            [
                g["valid"],
                o["valid"],
                fx["valid"],
                b["valid"],
                dr["valid"],
                si["valid"],
            ]
        )
        confidence = {6: "高", 5: "中", 4: "中", 3: "低"}.get(valid_count, "低")
        lines += [
            "",
            "---",
            f"📊 数据置信度: **{confidence}** ({valid_count}/6 维度有效)",
        ]

        return {
            "header": {
                "title": {"tag": "plain_text", "content": "🌏 全球隔夜简报"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
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
        """构建纯文本摘要（用于 LLM prompt 上下文 — 大师兄解读）。"""
        d = snapshot.to_dict()
        parts = [
            f"【宏观日报 {snapshot.timestamp[:10]}】",
        ]
        # A 股大盘数据（大师兄解读重点）— 仅包含有效指数
        si = d["stock_index"]
        if si["valid"]:
            idx_parts = []
            if si.get("sh_index", 0) > 0:
                idx_parts.append(f"上证{si['sh_index']:.2f}({si['sh_change_pct']:+.2f}%)")
            if si.get("sz_index", 0) > 0:
                idx_parts.append(f"深证{si['sz_index']:.2f}({si['sz_change_pct']:+.2f}%)")
            if si.get("cyb_index", 0) > 0:
                idx_parts.append(f"创业板{si['cyb_index']:.2f}({si['cyb_change_pct']:+.2f}%)")
            if idx_parts:
                parts.append(f"A股: {' '.join(idx_parts)}")
            if si.get("total_volume", 0) > 0:
                parts.append(f"成交额{si['total_volume']:.0f}亿")
            if si.get("limit_up_count", 0) > 0:
                parts.append(f"涨停{si['limit_up_count']}家")
        # 北向资金（None 或 0 均视为获取不到）
        nf = d["derivative"].get("north_flow")
        if nf is not None and nf != 0:
            parts.append(f"北向资金{nf:.1f}亿")
        else:
            parts.append("北向资金获取不到")
        # 全球宏观
        if d["gold"]["valid"]:
            parts.append(f"黄金${d['gold']['gold_price']}")
        if d["oil"]["valid"]:
            parts.append(f"布伦特${d['oil']['brent_price']}")
        if d["bond"]["valid"]:
            parts.append(f"美债10Y {d['bond']['us10y_yield']}%")
            if d["bond"].get("inverted"):
                parts.append("美债倒挂⚠️")
        if d["fx"]["valid"]:
            parts.append(f"美元指数{d['fx']['usd_index']:.2f}")
            parts.append(f"USD/CNY {d['fx']['usd_cny']:.2f}")
        if d["derivative"]["valid"]:
            parts.append(f"BDI {d['derivative']['bdi']:.0f}")
            if d["derivative"].get("vix"):
                parts.append(f"VIX {d['derivative']['vix']:.1f}")
        return " | ".join(parts) if parts else "宏观数据暂不可用"
