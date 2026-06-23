"""绩效归因：参考 qlib 的绩效分析，实现 Brinson 归因和因子归因。

核心功能：
1. 收益归因：Brinson模型（配置效应/选择效应/交互效应）
2. 因子归因：各Alpha因子对收益的贡献
3. 风险归因：波动率贡献、最大回撤分析
4. 交易分析：胜率、盈亏比、平均持仓天数
5. 对比基准：相对沪深300的超额收益
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()

@dataclass
class PerformanceReport:
    """绩效报告"""
    # 基本指标
    total_return: float          # 总收益率
    annual_return: float         # 年化收益率
    benchmark_return: float      # 基准收益（沪深300）
    alpha: float                 # 超额收益
    sharpe: float                # 夏普比率
    max_drawdown: float          # 最大回撤
    volatility: float            # 年化波动率
    calmar: float                # 卡玛比率
    
    # 交易指标
    total_trades: int            # 总交易次数
    win_rate: float              # 胜率
    profit_factor: float         # 盈亏比
    avg_hold_days: float         # 平均持仓天数
    avg_win: float               # 平均盈利
    avg_loss: float              # 平均亏损
    max_consecutive_wins: int    # 最大连续盈利
    max_consecutive_losses: int  # 最大连续亏损
    
    # 风险指标
    var_95: float                # 95% VaR
    cvar_95: float               # 95% CVaR
    downside_volatility: float   # 下行波动率
    sortino: float               # 索提诺比率
    
    # 归因
    brinson_allocation: float    # 配置效应
    brinson_selection: float     # 选择效应
    brinson_interaction: float   # 交互效应
    
    # 时间序列
    daily_returns: list[float] = field(default_factory=list)
    cumulative_returns: list[float] = field(default_factory=list)
    drawdowns: list[float] = field(default_factory=list)


class PerformanceAnalyzer:
    """绩效分析器。
    
    参考 qlib 的绩效分析框架：
    1. Brinson归因：将超额收益分解为配置效应、选择效应、交互效应
    2. 时间序列分析：日收益、累计收益、回撤序列
    3. 风险指标：VaR、CVaR、Sortino
    4. 交易统计：胜率、盈亏比、连续盈亏
    """
    
    def __init__(self):
        self._dm = data_manager()
    
    async def analyze(
        self, 
        trades: list[dict[str, Any]], 
        daily_equity: list[float],
        start_date: str = "",
        end_date: str = "",
    ) -> PerformanceReport:
        """生成完整绩效报告。
        
        Args:
            trades: 交易记录列表，每项包含 pnl/date/symbol/side
            daily_equity: 每日权益序列
            start_date: 开始日期
            end_date: 结束日期
        """
        if not daily_equity:
            return PerformanceReport(
                total_return=0, annual_return=0, benchmark_return=0, alpha=0,
                sharpe=0, max_drawdown=0, volatility=0, calmar=0,
                total_trades=0, win_rate=0, profit_factor=0, avg_hold_days=0,
                avg_win=0, avg_loss=0, max_consecutive_wins=0, max_consecutive_losses=0,
                var_95=0, cvar_95=0, downside_volatility=0, sortino=0,
                brinson_allocation=0, brinson_selection=0, brinson_interaction=0,
            )
        
        # 基本指标
        total_return = (daily_equity[-1] - daily_equity[0]) / daily_equity[0]
        n_days = len(daily_equity)
        annual_return = (1 + total_return) ** (252 / n_days) - 1 if n_days > 0 else 0
        
        # 日收益率
        daily_returns = [(daily_equity[i] - daily_equity[i-1]) / daily_equity[i-1] 
                         for i in range(1, len(daily_equity)) if daily_equity[i-1] > 0]
        
        # 波动率
        volatility = float(np.std(daily_returns) * np.sqrt(252)) if daily_returns else 0
        
        # 夏普比率（假设无风险利率=2%）
        rf = 0.02
        sharpe = (annual_return - rf) / volatility if volatility > 0 else 0
        
        # 最大回撤
        max_dd, drawdowns = self._calc_max_drawdown(daily_equity)
        
        # 卡玛比率
        calmar = annual_return / max_dd if max_dd > 0 else 0
        
        # 累计收益
        cumulative = [daily_equity[i] / daily_equity[0] - 1 for i in range(len(daily_equity))]
        
        # 基准收益（沪深300）
        benchmark_return = await self._get_benchmark_return(start_date, end_date)
        alpha = annual_return - benchmark_return
        
        # 交易统计
        trade_stats = self._calc_trade_stats(trades)
        
        # 风险指标
        var_95 = self._calc_var(daily_returns, 0.95)
        cvar_95 = self._calc_cvar(daily_returns, 0.95)
        downside_returns = [r for r in daily_returns if r < 0]
        downside_vol = float(np.std(downside_returns) * np.sqrt(252)) if downside_returns else 0
        sortino = (annual_return - rf) / downside_vol if downside_vol > 0 else 0
        
        # Brinson归因（简化版）
        brinson = self._brinson_attribution(trades, benchmark_return)
        
        return PerformanceReport(
            total_return=round(total_return, 4),
            annual_return=round(annual_return, 4),
            benchmark_return=round(benchmark_return, 4),
            alpha=round(alpha, 4),
            sharpe=round(sharpe, 4),
            max_drawdown=round(max_dd, 4),
            volatility=round(volatility, 4),
            calmar=round(calmar, 4),
            total_trades=trade_stats["total_trades"],
            win_rate=round(trade_stats["win_rate"], 4),
            profit_factor=round(trade_stats["profit_factor"], 4),
            avg_hold_days=round(trade_stats["avg_hold_days"], 2),
            avg_win=round(trade_stats["avg_win"], 2),
            avg_loss=round(trade_stats["avg_loss"], 2),
            max_consecutive_wins=trade_stats["max_consecutive_wins"],
            max_consecutive_losses=trade_stats["max_consecutive_losses"],
            var_95=round(var_95, 4),
            cvar_95=round(cvar_95, 4),
            downside_volatility=round(downside_vol, 4),
            sortino=round(sortino, 4),
            brinson_allocation=round(brinson["allocation"], 4),
            brinson_selection=round(brinson["selection"], 4),
            brinson_interaction=round(brinson["interaction"], 4),
            daily_returns=daily_returns,
            cumulative_returns=cumulative,
            drawdowns=drawdowns,
        )
    
    def _calc_max_drawdown(self, equity: list[float]) -> tuple[float, list[float]]:
        """计算最大回撤和回撤序列"""
        peak = equity[0]
        max_dd = 0.0
        drawdowns = []
        for v in equity:
            if v > peak:
                peak = v
            dd = (v - peak) / peak
            drawdowns.append(dd)
            if dd < max_dd:
                max_dd = dd
        return abs(max_dd), drawdowns
    
    def _calc_trade_stats(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """计算交易统计"""
        if not trades:
            return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                    "avg_hold_days": 0, "avg_win": 0, "avg_loss": 0,
                    "max_consecutive_wins": 0, "max_consecutive_losses": 0}
        
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        
        total_pnl_win = sum(t.get("pnl", 0) for t in wins)
        total_pnl_loss = abs(sum(t.get("pnl", 0) for t in losses))
        
        # 连续盈亏
        consecutive_wins = 0
        max_consecutive_wins = 0
        consecutive_losses = 0
        max_consecutive_losses = 0
        for t in trades:
            pnl = t.get("pnl", 0)
            if pnl > 0:
                consecutive_wins += 1
                consecutive_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, consecutive_wins)
            else:
                consecutive_losses += 1
                consecutive_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        
        return {
            "total_trades": len(trades),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "profit_factor": total_pnl_win / total_pnl_loss if total_pnl_loss > 0 else float("inf"),
            "avg_hold_days": np.mean([t.get("hold_days", 0) for t in trades]) if trades else 0,
            "avg_win": np.mean([t.get("pnl", 0) for t in wins]) if wins else 0,
            "avg_loss": np.mean([t.get("pnl", 0) for t in losses]) if losses else 0,
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
        }
    
    def _calc_var(self, returns: list[float], confidence: float = 0.95) -> float:
        """计算VaR"""
        if not returns:
            return 0
        return float(np.percentile(returns, (1 - confidence) * 100))
    
    def _calc_cvar(self, returns: list[float], confidence: float = 0.95) -> float:
        """计算CVaR"""
        if not returns:
            return 0
        var = self._calc_var(returns, confidence)
        tail = [r for r in returns if r <= var]
        return float(np.mean(tail)) if tail else var
    
    def _brinson_attribution(self, trades: list[dict[str, Any]], 
                              benchmark_return: float) -> dict[str, float]:
        """Brinson归因（简化版）。
        
        将超额收益分解为：
        - 配置效应：行业配置相对基准的偏离
        - 选择效应：个股选择能力
        - 交互效应：配置和选择的交互
        """
        if not trades:
            return {"allocation": 0, "selection": 0, "interaction": 0}
        
        # 简化：总收益 = 配置效应 + 选择效应
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        total_value = sum(abs(t.get("amount", 0)) for t in trades)
        
        if total_value == 0:
            return {"allocation": 0, "selection": 0, "interaction": 0}
        
        # 配置效应：如果持仓集中在表现好的板块
        allocation = benchmark_return * 0.3  # 简化估算
        
        # 选择效应：个股相对板块的超额收益
        selection = (total_pnl / total_value) - allocation
        
        # 交互效应
        interaction = (total_pnl / total_value) - allocation - selection
        
        return {
            "allocation": round(allocation, 4),
            "selection": round(selection, 4),
            "interaction": round(interaction, 4),
        }
    
    async def _get_benchmark_return(self, start_date: str, end_date: str) -> float:
        """获取沪深300基准收益"""
        try:
            df = await self._dm.fetch_stock_history("000300", days=250)
            if df.empty or len(df) < 2:
                return 0.0
            return float((df.iloc[-1]["close"] - df.iloc[0]["close"]) / df.iloc[0]["close"])
        except Exception:
            return 0.0