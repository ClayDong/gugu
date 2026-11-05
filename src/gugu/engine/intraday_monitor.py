"""v0.5.0 盘中实时盯盘监控器。

按性能分级频率扫描持仓 + watchlist 的资金流异动，
发现大单异动、连续资金流、行业异动、持仓预警时推送告警。

设计原则：
- 性能自适应：按 CPU/内存自动选择扫描频率和范围
- 非交易时段跳过
- 告警去重：同一信号 5 分钟内不重复推送
- 不修改 SignalPipeline，独立运行
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gugu.data.manager import DataManager
from gugu.utils.calendar import is_trading_day, is_trading_time
from gugu.utils.log import get_logger

logger = get_logger()


def detect_performance_mode() -> str:
    """自动检测性能模式。

    按 CPU 核数和内存容量分级：
    - high:   CPU>=8 且 内存>=16GB
    - medium: CPU>=4 且 内存>=8GB
    - low:    其他

    psutil 未安装时降级到 ``medium``，避免硬依赖。
    """
    try:
        import psutil
    except ImportError:
        logger.debug("psutil 未安装，detect_performance_mode 降级到 medium")
        return "medium"

    cpu_count = os.cpu_count() or 4
    mem = psutil.virtual_memory()
    mem_gb = mem.total / (1024**3)

    if cpu_count >= 8 and mem_gb >= 16:
        return "high"
    elif cpu_count >= 4 and mem_gb >= 8:
        return "medium"
    else:
        return "low"


PERFORMANCE_CONFIG = {
    "high": {"interval_sec": 30, "max_symbols": 50, "scan_watchlist": True},
    "medium": {"interval_sec": 120, "max_symbols": 30, "scan_watchlist": True},
    "low": {"interval_sec": 300, "max_symbols": 5, "scan_watchlist": False},
    "off": {"interval_sec": 0, "max_symbols": 0, "scan_watchlist": False},
}


@dataclass
class AlertItem:
    """异动告警条目。"""

    alert_type: str  # "super_large_inflow" | "super_large_outflow" | "main_pct_high" | "main_pct_low" | "consecutive_inflow" | "consecutive_outflow" | "industry_inflow" | "industry_outflow" | "industry_divergence" | "position_warning"
    symbol: str
    name: str
    severity: str  # "info" | "warning" | "critical"
    direction: str  # "buy" | "sell" | "neutral"
    value: float  # 数值（净流入额、占比等）
    message: str  # 人类可读消息
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class IntradayMonitor:
    """盘中实时资金流异动监控器。

    按性能分级频率扫描持仓 + watchlist 的资金流和实时行情，
    发现大单异动、连续资金流、行业异动、持仓预警时推送告警。

    设计原则：
    - 性能自适应：按 CPU/内存自动选择扫描频率和范围
    - 非交易时段跳过
    - 告警去重：同一信号 5 分钟内不重复推送
    - 不修改 SignalPipeline，独立运行
    """

    ALERT_COOLDOWN_SEC = 300  # 同一信号 5 分钟内不重复推送

    def _cleanup_alert_history(self) -> None:
        """清理过期的告警冷却记录，防止 _alert_history 字典无限增长。"""
        if not self._alert_history:
            return
        now = time.time()
        stale_keys = [
            k for k, ts in self._alert_history.items() if now - ts >= self.ALERT_COOLDOWN_SEC * 2
        ]
        for k in stale_keys:
            del self._alert_history[k]

    def __init__(
        self,
        data_manager: DataManager | None = None,
        watchlist: list[str] | None = None,
        portfolio_symbols: list[str] | None = None,
        performance_mode: str = "auto",  # auto|high|medium|low|off
        alert_thresholds: dict[str, Any] | None = None,
    ) -> None:
        self._dm = data_manager or data_manager()
        self._watchlist = watchlist or []
        self._portfolio_symbols = portfolio_symbols or []
        # 解析性能模式
        if performance_mode == "auto":
            self._mode = detect_performance_mode()
        else:
            self._mode = performance_mode
        self._config = PERFORMANCE_CONFIG.get(self._mode, PERFORMANCE_CONFIG["medium"])
        # 默认告警阈值（单位：亿元，与 DataManager 返回的单位对齐）
        # P0-1 修复：原阈值按元单位配置（100_000_000），但数据源已 /1e8 转为亿元，
        # 导致 1.5（亿元）> 100_000_000 永远 False，整个盘中监控完全失效
        self._thresholds = alert_thresholds or {
            "super_large_inflow": 1.0,  # 1 亿
            "super_large_outflow": 1.0,
            "main_pct_threshold": 5.0,  # 5%（主力净占比，单位：%）
            "consecutive_days": 3,
            "consecutive_total": 3.0,  # 连续累计 3 亿
            "industry_flow_threshold": 10.0,  # 行业 10 亿
            "position_outflow_warning": 0.5,  # 持仓股主力净流出超 0.5 亿告警
        }
        # 告警冷却记录：{(alert_type, symbol): last_alert_ts}
        self._alert_history: dict[tuple[str, str], float] = {}

    def get_scan_symbols(self) -> list[str]:
        """获取本次扫描的股票列表。"""
        max_n = self._config["max_symbols"]
        symbols = list(self._portfolio_symbols)  # 持仓优先
        if self._config["scan_watchlist"]:
            for s in self._watchlist:
                if s not in symbols:
                    symbols.append(s)
        return symbols[:max_n]

    def should_run(self) -> bool:
        """判断是否应该执行扫描。"""
        if self._mode == "off":
            return False
        # P2 修复：每次扫描前清理过期告警历史，防止字典无限增长
        self._cleanup_alert_history()
        if not is_trading_day():
            return False
        return is_trading_time()

    async def run_cycle(self) -> list[AlertItem]:
        """执行一次扫描周期，返回异动告警列表。"""
        if not self.should_run():
            return []

        alerts: list[AlertItem] = []
        symbols = self.get_scan_symbols()
        if not symbols:
            return []

        logger.info("盘中扫描启动: 模式={}, 股票数={}", self._mode, len(symbols))

        # 1. 个股资金流扫描
        for symbol in symbols:
            try:
                stock_alerts = await self._scan_stock(symbol)
                alerts.extend(stock_alerts)
            except Exception as e:
                logger.debug("扫描 {} 失败: {}", symbol, e)

        # 2. 行业资金流扫描（每次都执行，单次 API 调用）
        try:
            industry_alerts = await self._scan_industries()
            alerts.extend(industry_alerts)
        except Exception as e:
            logger.debug("行业扫描失败: {}", e)

        # 3. 过滤冷却中的告警
        filtered = self._filter_cooldown(alerts)

        if filtered:
            logger.info("盘中扫描完成: 发现 {} 条异动告警", len(filtered))

        return filtered

    async def _scan_stock(self, symbol: str) -> list[AlertItem]:
        """扫描单股资金流异动。"""
        alerts: list[AlertItem] = []

        try:
            df = await self._dm.fetch_stock_flow(symbol)
            if df is None or df.empty:
                return []

            # 取最新日数据
            latest = df.iloc[-1]
            super_large_net = float(latest.get("super_large_net", 0) or 0)
            main_net = float(latest.get("main_net", 0) or 0)
            main_pct = float(latest.get("main_pct", 0) or 0)

            # 查询股票名称
            from gugu.data.stock_names import lookup_stock_name

            name = lookup_stock_name(symbol) or symbol

            # 告警 1：超大单净流入 > 1 亿
            # P0-1 修复：super_large_net 已是亿元单位，不再 /1e8
            if super_large_net > self._thresholds["super_large_inflow"]:
                alerts.append(
                    AlertItem(
                        alert_type="super_large_inflow",
                        symbol=symbol,
                        name=name,
                        severity="info",
                        direction="buy",
                        value=super_large_net,
                        message=f"🔴 {name}({symbol}) 超大单净流入 {super_large_net:.2f}亿",
                    )
                )

            # 告警 2：超大单净流出 > 1 亿
            if super_large_net < -self._thresholds["super_large_outflow"]:
                alerts.append(
                    AlertItem(
                        alert_type="super_large_outflow",
                        symbol=symbol,
                        name=name,
                        severity="warning",
                        direction="sell",
                        value=super_large_net,
                        message=f"🟢 {name}({symbol}) 超大单净流出 {abs(super_large_net):.2f}亿",
                    )
                )

            # 告警 3：主力净占比 > 5%
            # P0-1 修复：main_pct 已是百分比单位（5.0 = 5%），阈值也用百分比单位
            if main_pct > self._thresholds["main_pct_threshold"]:
                alerts.append(
                    AlertItem(
                        alert_type="main_pct_high",
                        symbol=symbol,
                        name=name,
                        severity="info",
                        direction="buy",
                        value=main_pct,
                        message=f"🔴 {name}({symbol}) 主力净占比 {main_pct:.2f}%",
                    )
                )

            # 告警 4：主力净占比 < -5%
            if main_pct < -self._thresholds["main_pct_threshold"]:
                alerts.append(
                    AlertItem(
                        alert_type="main_pct_low",
                        symbol=symbol,
                        name=name,
                        severity="warning",
                        direction="sell",
                        value=main_pct,
                        message=f"🟢 {name}({symbol}) 主力净占比 {main_pct:.2f}%",
                    )
                )

            # 告警 5/6：连续 N 日主力净流入/流出
            consecutive_days = int(self._thresholds.get("consecutive_days", 3))
            if len(df) >= consecutive_days:
                recent = df.tail(consecutive_days)
                recent_main_net = recent["main_net"].sum() if "main_net" in recent.columns else 0
                all_positive = (
                    (recent["main_net"] > 0).all() if "main_net" in recent.columns else False
                )
                all_negative = (
                    (recent["main_net"] < 0).all() if "main_net" in recent.columns else False
                )
                consecutive_total = float(self._thresholds.get("consecutive_total", 3.0))

                if all_positive and recent_main_net > consecutive_total:
                    alerts.append(
                        AlertItem(
                            alert_type="consecutive_inflow",
                            symbol=symbol,
                            name=name,
                            severity="info",
                            direction="buy",
                            value=recent_main_net,
                            message=(
                                f"🔴 {name}({symbol}) 连续{consecutive_days}日主力净流入"
                                f"累计 {recent_main_net:.2f}亿"
                            ),
                        )
                    )

                if all_negative and recent_main_net < -consecutive_total:
                    alerts.append(
                        AlertItem(
                            alert_type="consecutive_outflow",
                            symbol=symbol,
                            name=name,
                            severity="critical",
                            direction="sell",
                            value=recent_main_net,
                            message=(
                                f"🟢 {name}({symbol}) 连续{consecutive_days}日主力净流出"
                                f"累计 {abs(recent_main_net):.2f}亿"
                            ),
                        )
                    )

            # 告警 7：持仓股主力净流出超 5000 万
            # P0-1 修复：使用配置阈值，单位亿元
            position_warning_threshold = float(
                self._thresholds.get("position_outflow_warning", 0.5)
            )
            if symbol in self._portfolio_symbols and main_net < -position_warning_threshold:
                alerts.append(
                    AlertItem(
                        alert_type="position_warning",
                        symbol=symbol,
                        name=name,
                        severity="critical",
                        direction="sell",
                        value=main_net,
                        message=(
                            f"🟢 持仓 {name}({symbol}) 主力净流出{abs(main_net):.2f}亿，建议关注"
                        ),
                    )
                )

        except Exception as e:
            logger.debug("扫描 {} 异常: {}", symbol, e)

        return alerts

    async def _scan_industries(self) -> list[AlertItem]:
        """扫描行业资金流异动。"""
        alerts: list[AlertItem] = []

        try:
            df = await self._dm.fetch_sector_flow()
            if df is None or df.empty:
                return []

            threshold = float(self._thresholds.get("industry_flow_threshold", 10.0))

            for _, row in df.iterrows():
                sector = str(row.get("sector", ""))
                main_net = float(row.get("main_net", 0) or 0)
                change_pct = float(row.get("change_pct", 0) or 0)

                if not sector:
                    continue

                # 行业流入 > 10 亿
                # P0-1 修复：main_net 已是亿元单位，不再 /1e8
                if main_net > threshold:
                    alerts.append(
                        AlertItem(
                            alert_type="industry_inflow",
                            symbol=sector,
                            name=sector,
                            severity="info",
                            direction="buy",
                            value=main_net,
                            message=(
                                f"🔴 行业 {sector} 资金流入 {main_net:.2f}亿"
                                f"（涨幅 {change_pct:.2f}%）"
                            ),
                        )
                    )

                # 行业流出 > 10 亿
                if main_net < -threshold:
                    alerts.append(
                        AlertItem(
                            alert_type="industry_outflow",
                            symbol=sector,
                            name=sector,
                            severity="warning",
                            direction="sell",
                            value=main_net,
                            message=(
                                f"🟢 行业 {sector} 资金流出 {abs(main_net):.2f}亿"
                                f"（涨幅 {change_pct:.2f}%）"
                            ),
                        )
                    )

                # 顶背离：资金流出但行业上涨
                if main_net < -threshold / 2 and change_pct > 1.0:
                    alerts.append(
                        AlertItem(
                            alert_type="industry_divergence",
                            symbol=sector,
                            name=sector,
                            severity="warning",
                            direction="neutral",
                            value=main_net,
                            message=(
                                f"⚠️ 行业 {sector} 顶背离：上涨 {change_pct:.2f}%"
                                f"但资金流出 {abs(main_net):.2f}亿"
                            ),
                        )
                    )

                # 底背离：资金流入但行业下跌
                if main_net > threshold / 2 and change_pct < -1.0:
                    alerts.append(
                        AlertItem(
                            alert_type="industry_divergence",
                            symbol=sector,
                            name=sector,
                            severity="info",
                            direction="buy",
                            value=main_net,
                            message=(
                                f"⚠️ 行业 {sector} 底背离：下跌 {change_pct:.2f}%"
                                f"但资金流入 {main_net:.2f}亿（可能吸筹）"
                            ),
                        )
                    )

        except Exception as e:
            logger.debug("行业扫描异常: {}", e)

        return alerts

    def _filter_cooldown(self, alerts: list[AlertItem]) -> list[AlertItem]:
        """过滤冷却中的告警（同一信号 5 分钟内不重复推送）。"""
        now = time.time()
        filtered: list[AlertItem] = []

        for alert in alerts:
            key = (alert.alert_type, alert.symbol)
            last_ts = self._alert_history.get(key, 0)
            if now - last_ts >= self.ALERT_COOLDOWN_SEC:
                filtered.append(alert)
                self._alert_history[key] = now

        return filtered

    def describe(self) -> dict[str, Any]:
        """返回监控器配置描述。"""
        return {
            "mode": self._mode,
            "interval_sec": self._config["interval_sec"],
            "max_symbols": self._config["max_symbols"],
            "scan_watchlist": self._config["scan_watchlist"],
            "watchlist_size": len(self._watchlist),
            "portfolio_size": len(self._portfolio_symbols),
            "thresholds": self._thresholds,
        }
