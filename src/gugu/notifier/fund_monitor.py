"""基金监控模块 — 财通成长优选混合A(001480)等基金净值跟踪。

移植自 MingCe bot/services/fund_monitor.py，适配 gugu 架构：
- 配置统一从 settings.yaml 读取（非 SQLite）
- 使用 gugu 的 logger / config 基础设施
- 卡片走 gugu 的 FeishuNotifier

功能：
- 获取基金单位净值（akshare）
- 计算日/周/月/季/年涨跌幅
- 监控日涨跌幅（定投调整建议）
- 监控累计收益（止盈信号）
- 监控回撤（加仓信号）
- 监控波动率（风控建议）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import akshare as ak
import numpy as np
import pandas as pd

from gugu.config import settings
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class FundConfig:
    """单只基金的监控配置。"""
    fund_code: str = "001480"
    fund_name: str = "财通成长优选混合A"
    # 以下为可选持仓信息（用于计算盈亏/止盈/回撤）
    cost_price: Optional[float] = None
    total_shares: Optional[float] = None
    total_investment: Optional[float] = None
    # 基础定投金额（元）
    base_investment: float = 1000.0


@dataclass
class FundData:
    """基金净值与涨跌幅数据。"""
    date: str
    net_value: float
    daily_change_pct: float
    weekly_change_pct: Optional[float] = None
    monthly_change_pct: Optional[float] = None
    quarterly_change_pct: Optional[float] = None
    yearly_change_pct: Optional[float] = None


@dataclass
class MonitorAlert:
    """监控告警。"""
    alert_type: str  # daily_change / profit / drawdown / volatility
    level: str       # info / warning / danger
    title: str
    content: str
    action: Optional[str] = None
    timestamp: str = ""


class FundMonitor:
    """单只基金监控器。"""

    def __init__(self, config: Optional[FundConfig] = None) -> None:
        self.config = config or FundConfig()
        self.fund_data: Optional[FundData] = None
        self.history_data: Optional[pd.DataFrame] = None
        self.alerts: list[MonitorAlert] = []

    # ── 数据采集 ──────────────────────────────────────────────────

    def fetch_fund_data(self) -> Optional[FundData]:
        """获取基金净值数据（同步，akshare 阻塞调用）。"""
        logger.info(f"获取基金数据: {self.config.fund_code} ({self.config.fund_name})")
        try:
            df = ak.fund_open_fund_info_em(
                symbol=self.config.fund_code, indicator="单位净值走势"
            )
            if df is None or df.empty:
                logger.warning(f"无数据: {self.config.fund_code}")
                return None

            df = df.sort_values("净值日期", ascending=False)
            latest = df.iloc[0]
            date = str(latest.get("净值日期", ""))
            net_value = float(latest.get("单位净值", 0))

            # 计算日涨跌幅
            daily_change = 0.0
            if len(df) >= 2:
                prev = float(df.iloc[1].get("单位净值", 0))
                if prev > 0:
                    daily_change = ((net_value - prev) / prev) * 100

            self.history_data = df
            self.fund_data = FundData(
                date=date,
                net_value=net_value,
                daily_change_pct=daily_change,
                weekly_change_pct=self._calc_period_change(df, 5),
                monthly_change_pct=self._calc_period_change(df, 20),
                quarterly_change_pct=self._calc_period_change(df, 60),
                yearly_change_pct=self._calc_period_change(df, 250),
            )

            logger.info(
                f"基金数据: {self.fund_data.date} "
                f"净值={self.fund_data.net_value:.4f} "
                f"日涨跌={self.fund_data.daily_change_pct:+.2f}%"
            )
            return self.fund_data

        except Exception as e:
            logger.error(f"获取基金数据失败 {self.config.fund_code}: {e}")
            return None

    @staticmethod
    def _calc_period_change(df: pd.DataFrame, days: int) -> Optional[float]:
        """计算过去 days 天的涨跌幅。"""
        if len(df) < days:
            return None
        try:
            cur = float(df.iloc[0].get("单位净值", 0))
            prv = float(df.iloc[days].get("单位净值", 0))
            if prv > 0:
                return ((cur - prv) / prv) * 100
        except Exception:
            pass
        return None

    # ── 分析指标 ──────────────────────────────────────────────────

    def calc_profit(self) -> Optional[float]:
        """计算累计收益率（需配置 cost_price）。"""
        if self.fund_data is None or not self.config.cost_price or self.config.cost_price <= 0:
            return None
        return ((self.fund_data.net_value - self.config.cost_price) / self.config.cost_price) * 100

    def calc_drawdown(self) -> Optional[float]:
        """计算当前回撤率（从历史最高净值算起）。"""
        if self.history_data is None or self.history_data.empty or self.fund_data is None:
            return None
        try:
            peak = float(self.history_data["单位净值"].max())
            cur = self.fund_data.net_value
            if peak > 0:
                return ((cur - peak) / peak) * 100
        except Exception:
            pass
        return None

    def calc_volatility(self, days: int = 20) -> Optional[float]:
        """计算年化波动率。"""
        if self.history_data is None or len(self.history_data) < days:
            return None
        try:
            recent = self.history_data.head(days)
            vals = recent["单位净值"].astype(float)
            returns = vals.pct_change().dropna()
            if len(returns) < 5:
                return 0.0
            return float(returns.std() * 100 * np.sqrt(250))
        except Exception:
            return None

    # ── 监控规则 ──────────────────────────────────────────────────

    def _now_ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def monitor_daily_change(self) -> list[MonitorAlert]:
        """日涨跌幅监控。"""
        alerts: list[MonitorAlert] = []
        if self.fund_data is None:
            return alerts
        dc = self.fund_data.daily_change_pct

        # 下跌
        if dc <= -10.0:
            alerts.append(MonitorAlert("daily_change", "danger",
                f"日跌幅超10%: {dc:.2f}%",
                f"建议定投金额×3.0 ({self.config.base_investment * 3:.0f}元)",
                "加仓", self._now_ts()))
        elif dc <= -8.0:
            alerts.append(MonitorAlert("daily_change", "danger",
                f"日跌幅超8%: {dc:.2f}%",
                f"建议定投金额×2.0 ({self.config.base_investment * 2:.0f}元)",
                "加仓", self._now_ts()))
        elif dc <= -5.0:
            alerts.append(MonitorAlert("daily_change", "warning",
                f"日跌幅超5%: {dc:.2f}%",
                f"建议定投金额×1.5 ({self.config.base_investment * 1.5:.0f}元)",
                "加仓", self._now_ts()))
        elif dc <= -3.0:
            alerts.append(MonitorAlert("daily_change", "warning",
                f"日跌幅超3%: {dc:.2f}%",
                f"建议定投金额×1.2 ({self.config.base_investment * 1.2:.0f}元)",
                "加仓", self._now_ts()))
        # 上涨
        if dc >= 8.0:
            alerts.append(MonitorAlert("daily_change", "info",
                f"日涨幅超8%: {dc:.2f}%",
                "建议暂停定投1-2日", "暂停定投", self._now_ts()))
        elif dc >= 5.0:
            alerts.append(MonitorAlert("daily_change", "info",
                f"日涨幅超5%: {dc:.2f}%",
                f"建议定投金额×0.6 ({self.config.base_investment * 0.6:.0f}元)",
                "减仓", self._now_ts()))
        elif dc >= 3.0:
            alerts.append(MonitorAlert("daily_change", "info",
                f"日涨幅超3%: {dc:.2f}%",
                f"建议定投金额×0.8 ({self.config.base_investment * 0.8:.0f}元)",
                "减仓", self._now_ts()))
        return alerts

    def monitor_profit(self) -> list[MonitorAlert]:
        """累计收益监控（止盈信号）。"""
        alerts: list[MonitorAlert] = []
        profit = self.calc_profit()
        if profit is None:
            return alerts
        if profit >= 200.0:
            alerts.append(MonitorAlert("profit", "danger",
                f"累计收益超200%: {profit:.2f}%", "建议全部止盈",
                "全部止盈", self._now_ts()))
        elif profit >= 150.0:
            alerts.append(MonitorAlert("profit", "warning",
                f"累计收益超150%: {profit:.2f}%", "建议止盈40%，保留60%",
                "分批止盈", self._now_ts()))
        elif profit >= 100.0:
            alerts.append(MonitorAlert("profit", "warning",
                f"累计收益超100%: {profit:.2f}%", "建议止盈30%，保留70%",
                "分批止盈", self._now_ts()))
        elif profit >= 50.0:
            alerts.append(MonitorAlert("profit", "info",
                f"累计收益超50%: {profit:.2f}%", "建议止盈30%，保留70%",
                "分批止盈", self._now_ts()))
        return alerts

    def monitor_drawdown(self) -> list[MonitorAlert]:
        """回撤监控。"""
        alerts: list[MonitorAlert] = []
        dd = self.calc_drawdown()
        if dd is None:
            return alerts
        if dd <= -25.0:
            alerts.append(MonitorAlert("drawdown", "danger",
                f"回撤超25%: {dd:.2f}%",
                f"建议增加150%定投金额 ({self.config.base_investment * 2.5:.0f}元)",
                "加仓", self._now_ts()))
        elif dd <= -15.0:
            alerts.append(MonitorAlert("drawdown", "warning",
                f"回撤超15%: {dd:.2f}%",
                f"建议增加100%定投金额 ({self.config.base_investment * 2:.0f}元)",
                "加仓", self._now_ts()))
        elif dd <= -10.0:
            alerts.append(MonitorAlert("drawdown", "warning",
                f"回撤超10%: {dd:.2f}%",
                f"建议增加50%定投金额 ({self.config.base_investment * 1.5:.0f}元)",
                "加仓", self._now_ts()))
        return alerts

    def monitor_volatility(self) -> list[MonitorAlert]:
        """波动率监控。"""
        alerts: list[MonitorAlert] = []
        vol = self.calc_volatility()
        if vol is None:
            return alerts
        if vol >= 60.0:
            alerts.append(MonitorAlert("volatility", "danger",
                f"波动率极高: {vol:.2f}%", "建议暂停定投，观察市场",
                "暂停定投", self._now_ts()))
        elif vol >= 50.0:
            alerts.append(MonitorAlert("volatility", "warning",
                f"波动率高: {vol:.2f}%",
                f"建议增加50%定投金额 ({self.config.base_investment * 1.5:.0f}元)",
                "加仓", self._now_ts()))
        elif vol >= 40.0:
            alerts.append(MonitorAlert("volatility", "info",
                f"波动率中等: {vol:.2f}%",
                f"建议增加20%定投金额 ({self.config.base_investment * 1.2:.0f}元)",
                "加仓", self._now_ts()))
        elif vol <= 30.0:
            alerts.append(MonitorAlert("volatility", "info",
                f"波动率低: {vol:.2f}%", "建议正常定投",
                "正常定投", self._now_ts()))
        return alerts

    # ── 完整监控 ──────────────────────────────────────────────────

    async def run_monitor(self) -> dict[str, Any]:
        """运行完整监控（异步包装同步采集）。"""
        logger.info(f"运行基金监控: {self.config.fund_name}")

        # 通过线程池运行同步的 akshare 调用
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, self.fetch_fund_data)
        if self.fund_data is None:
            logger.warning("无基金数据，跳过监控")
            return {"status": "error", "message": "无法获取基金数据"}

        self.alerts = []
        self.alerts.extend(self.monitor_daily_change())
        self.alerts.extend(self.monitor_profit())
        self.alerts.extend(self.monitor_drawdown())
        self.alerts.extend(self.monitor_volatility())
        self.alerts.sort(key=lambda a: {"danger": 0, "warning": 1, "info": 2}.get(a.level, 3))

        now = self._now_ts()
        result: dict[str, Any] = {
            "status": "success",
            "fund_code": self.config.fund_code,
            "fund_name": self.config.fund_name,
            "date": self.fund_data.date,
            "net_value": self.fund_data.net_value,
            "daily_change_pct": self.fund_data.daily_change_pct,
            "weekly_change_pct": self.fund_data.weekly_change_pct,
            "monthly_change_pct": self.fund_data.monthly_change_pct,
            "quarterly_change_pct": self.fund_data.quarterly_change_pct,
            "yearly_change_pct": self.fund_data.yearly_change_pct,
            "profit_pct": self.calc_profit(),
            "drawdown_pct": self.calc_drawdown(),
            "volatility": self.calc_volatility(),
            "alerts": [a.__dict__ for a in self.alerts],
            "alert_count": len(self.alerts),
            "timestamp": now,
        }

        # 提示未配置成本价
        warnings: list[str] = []
        if self.config.cost_price is None:
            warnings.append(f"💡 {self.config.fund_name} 未配置成本价（无法计算盈亏）")
        if warnings:
            result["warnings"] = warnings

        logger.info(f"基金监控完成: {len(self.alerts)} 条告警")
        return result


# ── 多基金管理 ──────────────────────────────────────────────────


def load_fund_configs() -> list[FundConfig]:
    """从 settings.yaml 加载基金列表。"""
    cfg = settings()
    fund_cfg = cfg.get("fund", {})
    if not fund_cfg.get("enabled", True):
        logger.info("基金监控已禁用")
        return []

    raw_funds = fund_cfg.get("funds", [])
    if not raw_funds:
        logger.warning("settings.yaml 中未配置基金列表")
        return []

    configs: list[FundConfig] = []
    for f in raw_funds:
        configs.append(FundConfig(
            fund_code=f.get("code", "001480"),
            fund_name=f.get("name", ""),
            cost_price=f.get("cost_price"),
            total_shares=f.get("total_shares"),
            total_investment=f.get("total_investment"),
            base_investment=f.get("base_investment", 1000.0),
        ))
    logger.info(f"加载 {len(configs)} 只基金配置: {[c.fund_name for c in configs]}")
    return configs


async def run_all_fund_monitors() -> dict[str, Any]:
    """运行所有已配置基金的监控。

    返回: {"results": [{...}], "date": "YYYY-MM-DD"}
    """
    configs = load_fund_configs()
    if not configs:
        return {"results": [], "date": datetime.now().strftime("%Y-%m-%d")}

    results: list[dict[str, Any]] = []
    latest_date = ""

    for cfg in configs:
        monitor = FundMonitor(config=cfg)
        try:
            result = await monitor.run_monitor()
            results.append(result)
            if result.get("status") == "success" and result.get("date"):
                latest_date = result["date"]
        except Exception as e:
            logger.error(f"监控基金 {cfg.fund_code} 失败: {e}")
            results.append({
                "status": "error",
                "fund_code": cfg.fund_code,
                "fund_name": cfg.fund_name,
                "message": str(e),
            })

    return {
        "results": results,
        "date": latest_date or datetime.now().strftime("%Y-%m-%d"),
    }


# ── 飞书卡片格式化 ──────────────────────────────────────────


def fmt_pct(val: Any) -> str:
    """格式化百分比，None 显示 '--'。"""
    if val is None:
        return "--"
    return f"{val:+.2f}%"


def build_fund_monitor_card(monitor_result: dict[str, Any]) -> dict[str, Any]:
    """构建基金监控飞书卡片（兼容 gugu 的 _card 结构）。"""
    from gugu.notifier.formatter import _card  # noqa: PLC0415

    fund_name = monitor_result.get("fund_name", "")
    fund_code = monitor_result.get("fund_code", "")
    date = monitor_result.get("date", "")
    net_value = monitor_result.get("net_value", 0)
    daily_change = monitor_result.get("daily_change_pct", 0)
    weekly_change = monitor_result.get("weekly_change_pct")
    monthly_change = monitor_result.get("monthly_change_pct")
    quarterly_change = monitor_result.get("quarterly_change_pct")
    yearly_change = monitor_result.get("yearly_change_pct")
    profit_pct = monitor_result.get("profit_pct")
    drawdown_pct = monitor_result.get("drawdown_pct")
    volatility = monitor_result.get("volatility")
    alerts = monitor_result.get("alerts", [])
    timestamp = monitor_result.get("timestamp", "")
    cost_warnings = monitor_result.get("warnings", [])

    icon = "🟢" if daily_change >= 0 else "🔴"
    market_status = "上涨" if daily_change >= 0 else "下跌"
    template = "blue" if daily_change >= 0 else "red"

    sections: list[str] = []

    # ── 基本信息 ──
    sections.append(
        f"📊 **{fund_name}** ({fund_code})\n"
        f"**净值日期**：{date}\n"
        f"**单位净值**：{net_value:.4f}  {icon} {fmt_pct(daily_change)} ({market_status})"
    )

    # ── 周期表现 ──
    period_lines: list[str] = []
    if weekly_change is not None:
        w = "📈" if weekly_change >= 0 else "📉"
        period_lines.append(f"{w} 近1周：{fmt_pct(weekly_change)}")
    if monthly_change is not None:
        m = "📈" if monthly_change >= 0 else "📉"
        period_lines.append(f"{m} 近1月：{fmt_pct(monthly_change)}")
    if quarterly_change is not None:
        q = "📈" if quarterly_change >= 0 else "📉"
        period_lines.append(f"{q} 近3月：{fmt_pct(quarterly_change)}")
    if yearly_change is not None:
        y = "📈" if yearly_change >= 0 else "📉"
        period_lines.append(f"{y} 近1年：{fmt_pct(yearly_change)}")
    if period_lines:
        sections.append("📅 **周期表现**\n" + "\n".join(period_lines))

    # ── 累计盈亏 ──
    if profit_pct is None:
        sections.append("💡 **未配置成本价**：使用 settings.yaml fund.funds[].cost_price 配置")
    else:
        p_icon = "💰" if profit_pct >= 0 else "📉"
        p_label = "盈利" if profit_pct >= 0 else "亏损"
        sections.append(f"{p_icon} **累计{p_label}**：{fmt_pct(profit_pct)}")

    # ── 回撤 ──
    if drawdown_pct is not None:
        if drawdown_pct <= -25:
            dd_icon, risk = "🔴", "高风险"
        elif drawdown_pct <= -15:
            dd_icon, risk = "🟡", "中等风险"
        elif drawdown_pct <= -10:
            dd_icon, risk = "🟢", "低风险"
        else:
            dd_icon, risk = "⚪", "正常"
        sections.append(f"{dd_icon} **当前回撤**：{fmt_pct(drawdown_pct)} ({risk})")

    # ── 波动率 ──
    if volatility is not None:
        if volatility >= 60:
            v_icon, v_sug = "🔴", "暂停定投"
        elif volatility >= 50:
            v_icon, v_sug = "🟡", "增加50%"
        elif volatility >= 40:
            v_icon, v_sug = "🟢", "增加20%"
        else:
            v_icon, v_sug = "⚪", "正常定投"
        sections.append(f"{v_icon} **波动率**：{volatility:.2f}% → 建议{v_sug}")

    # ── 告警 ──
    if alerts:
        danger = sum(1 for a in alerts if a.get("level") == "danger")
        warning = sum(1 for a in alerts if a.get("level") == "warning")
        info = sum(1 for a in alerts if a.get("level") == "info")
        summary_parts = []
        if danger > 0:
            summary_parts.append(f"🔴 {danger}个")
        if warning > 0:
            summary_parts.append(f"🟡 {warning}个")
        if info > 0:
            summary_parts.append(f"🟢 {info}个")
        if summary_parts:
            sections.append(f"🚨 **告警摘要**（{' '.join(summary_parts)})")

        alert_lines: list[str] = []
        for a in alerts[:5]:
            lv_icon = {"danger": "🔴", "warning": "🟡", "info": "🟢"}.get(a.get("level", "info"), "⚪")
            title = a.get("title", "")
            content = a.get("content", "")
            action = a.get("action", "")
            alert_lines.append(f"{lv_icon} **{title}**\n   {content}")
            if action:
                alert_lines.append(f"   👉 执行：{action}")
        sections.append("\n".join(alert_lines))

        # 最紧急操作建议
        urgent = [a.get("action", "") for a in alerts if a.get("level") in ("danger", "warning")]
        if urgent:
            sections.append(
                f"🎯 **本次建议**\n基于当前结果，建议：**{'/'.join(set(urgent[:3]))}**"
            )

    # ── 底部 ──
    note = f"明策 · gugu · 基金监控 | 📈 {timestamp} | ⚠️ 仅供参考"

    return _card(
        title=f"📊 {fund_name} 监控报告",
        template=template,
        sections=sections,
        note=note,
    )
