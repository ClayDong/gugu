"""统一事件研究框架（event-study v1）。

供所有策略共享，替代 scripts/ 下 9 个独立但重复的事件研究脚本。

方法论（gugu-strategy-backtest 铁律）：
1. 事件研究先行：信号后 N 日收益 vs 沪深300 超额，Welch t 检验
2. 训练期选参 / 验证期冻结（Walk-Forward）
3. 触发股集中度检查（单股>30%信号=不可信，剔除后重验）
4. 超额收益检验（赚钱≠有alpha）
5. 年份分桶（防单一年份/暴跌日污染）

使用方式:
    from gugu.analysis.event_study import EventStudy, StudyConfig

    config = StudyConfig(
        strategy_name="my_strategy",
        horizons=[3, 5, 10],
        index_code="000300",
    )
    study = EventStudy(config)
    result = study.run(signals_df)  # 从 signals_history.jsonl 加载
    # 或
    result = study.run_from_strategy(strategy, stock_pool)  # 直接调用策略
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()

# ── 默认参数 ─────────────────────────────────────────────

DEFAULT_HORIZONS = [3, 5, 10]
DEFAULT_TRAIN_PERIOD = ("2024-02-01", "2025-01-31")
DEFAULT_VAL_PERIOD = ("2025-02-01", "2026-07-31")
DEFAULT_CONCENTRATION_THRESHOLD = 0.30  # 单股信号占比 >30% 标记
DEFAULT_MIN_SAMPLES = 3  # t 检验最小样本
DEFAULT_COST_RATE = 0.004  # 双向交易成本（佣金+印花税+滑点）
DEFAULT_INDEX_CODE = "000300"


@dataclass
class StudyConfig:
    """事件研究配置。"""

    strategy_name: str
    horizons: list[int] = field(default_factory=lambda: DEFAULT_HORIZONS)
    index_code: str = DEFAULT_INDEX_CODE
    train_start: str = DEFAULT_TRAIN_PERIOD[0]
    train_end: str = DEFAULT_TRAIN_PERIOD[1]
    val_start: str = DEFAULT_VAL_PERIOD[0]
    val_end: str = DEFAULT_VAL_PERIOD[1]
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD
    min_samples: int = DEFAULT_MIN_SAMPLES
    cost_rate: float = DEFAULT_COST_RATE
    entry_mode: str = "next_open"  # "close" | "next_open"
    output_path: str | None = None  # 写入路径，None 不写文件
    stock_pool: list[str] | None = None  # 策略验证的股票池
    enrich_func: callable | None = None  # 策略特殊列注入函数，签名 (df, symbol) -> (df, dict)


@dataclass
class StudyEvent:
    """一次事件研究结果。"""

    symbol: str
    date: str  # signal date (entry)
    entry: float  # entry price
    period: str  # "train" | "val" | "outside"
    year: int  # signal year
    returns: dict[str, float] = field(default_factory=dict)  # {horizon: return}
    excess: dict[str, float] = field(default_factory=dict)  # {horizon: excess_return}


@dataclass
class HorizonResult:
    """单个时间维度的统计结果。"""

    horizon: int
    n_events: int
    avg_return: float
    avg_excess: float
    std_excess: float
    t_stat: float
    p_value: float
    win_rate: float
    sharpe: float
    excess_sharpe: float
    significant: bool  # p < 0.05


@dataclass
class StudyResult:
    """事件研究的完整结果。"""

    strategy_name: str
    config: StudyConfig
    n_events: int
    n_symbols: int
    concentration_issues: list[dict[str, Any]]
    per_horizon: list[HorizonResult]
    per_year: dict[int, list[HorizonResult]]
    per_period: dict[str, list[HorizonResult]]
    events: list[StudyEvent]  # 完整事件明细


# ── 工具函数 ─────────────────────────────────────────────


def welch_t(a: np.ndarray) -> tuple[float, float]:
    """对超额收益数组做单样本 Welch t 检验（vs 零）。"""
    from scipy import stats

    values = np.asarray(a, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3:
        return 0.0, 1.0
    result = stats.ttest_1samp(values, 0.0, nan_policy="omit")
    return float(result.statistic), float(result.pvalue)


def fetch_index_klines(index_code: str, days: int = 600) -> pd.DataFrame:
    """获取沪深300等指数 K 线（腾讯数据源）。"""
    import ssl
    import urllib.request

    ctx = ssl._create_unverified_context()
    tencent_kline_url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    sh_code = f"sh{index_code}" if index_code.startswith("000") else index_code

    url = f"{tencent_kline_url}?param={sh_code},day,,,{days},qfq"
    req = urllib.request.Request(
        url,
        headers={"Referer": "http://finance.qq.com/", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        logger.warning("指数 K 线获取失败: {}", e)
        return pd.DataFrame()

    node = data.get("data", {}).get(sh_code, {})
    kline = node.get("qfqday") or node.get("day") or []
    rows = []
    for row in kline:
        if len(row) < 6:
            continue
        rows.append(
            [row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_stock_klines(symbol: str, days: int = 600) -> pd.DataFrame:
    """获取个股 K 线（腾讯数据源）。"""
    import ssl
    import urllib.request

    ctx = ssl._create_unverified_context()
    tencent_kline_url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    tencent_market_map = {
        "6": "sh",
        "9": "sh",
        "0": "sz",
        "3": "sz",
        "4": "bj",
        "8": "bj",
    }
    prefix = tencent_market_map.get(symbol[0], "sh")
    full_code = f"{prefix}{symbol}"

    url = f"{tencent_kline_url}?param={full_code},day,,,{days},qfq"
    req = urllib.request.Request(
        url,
        headers={"Referer": "http://finance.qq.com/", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        logger.warning("个股 K 线获取失败 {}: {}", symbol, e)
        return pd.DataFrame()

    node = data.get("data", {}).get(full_code, {})
    kline = node.get("qfqday") or node.get("day") or []
    rows = []
    for row in kline:
        if len(row) < 6:
            continue
        rows.append(
            [row[0], float(row[1]), float(row[3]), float(row[4]), float(row[2]), float(row[5])]
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ── 核心分析类 ───────────────────────────────────────────


class EventStudy:
    """统一事件研究分析器。

    用法:
        config = StudyConfig(strategy_name="my_strat")
        study = EventStudy(config)

        # 方式 A: 从已有的信号 DataFrame 分析
        result = study.run(signals_df)

        # 方式 B: 从 signals_history.jsonl 加载分析
        result = study.run_from_history(history_path)

        # 方式 C: 运行策略 + 拉取股票池 K 线
        result = study.run_from_strategy(strategy_class, stock_pool)
    """

    def __init__(self, config: StudyConfig) -> None:
        self.config = config
        self._index_df: pd.DataFrame | None = None
        self._stock_cache: dict[str, pd.DataFrame] = {}

    # ── 入口方法 ─────────────────────────────────────────

    def run_from_history(
        self,
        history_path: str | Path = "data/signals_history.jsonl",
        max_days: int = 365,
    ) -> StudyResult:
        """从 signals_history.jsonl 读取信号并执行事件研究。

        适用于已通过 signal pipeline 的历史信号分析。
        """
        path = Path(history_path)
        if not path.exists():
            raise FileNotFoundError(f"信号历史文件不存在: {path}")

        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # 只取买入信号
        buy_signals = []
        for rec in records:
            if isinstance(rec.get("direction"), str) and rec["direction"].lower() != "buy":
                continue
            strategies = rec.get("strategies") or [rec.get("strategy")]
            if isinstance(strategies, str):
                strategies = [strategies]
            if self.config.strategy_name not in strategies:
                continue
            buy_signals.append(rec)

        if not buy_signals:
            logger.warning("策略 {} 在历史中无买入信号", self.config.strategy_name)
            return self._empty_result()

        # 构建 DataFrame
        rows = []
        for sig in buy_signals:
            ts = sig.get("timestamp")
            if not ts:
                continue
            try:
                sig_date = pd.Timestamp(ts)
            except (ValueError, TypeError):
                continue
            price = sig.get("order_price") or sig.get("price", 0)
            rows.append(
                {
                    "symbol": sig.get("symbol", ""),
                    "date": sig_date,
                    "entry": float(price) if price else 0.0,
                }
            )

        if not rows:
            return self._empty_result()

        df = pd.DataFrame(rows)
        df = df.dropna(subset=["symbol", "date"])
        df = df[df["entry"] > 0].reset_index(drop=True)

        return self.run(df)

    def run_from_strategy(
        self,
        strategy_class: type,
        stock_pool: list[str] | None = None,
        days: int = 600,
    ) -> StudyResult:
        """直接运行策略生成信号并执行事件研究。

        适用于新策略尚未有历史信号时的离线验证。

        支持 enrich_func 配置项：签名 (df, symbol) -> (df, dict)，用于注入
        策略依赖的特殊列（如 price_volume_signal 需要 amount/float_shares）。
        """
        pool = stock_pool or self.config.stock_pool or _DEFAULT_POOL
        all_events: list[dict] = []

        # 确保指数数据就绪
        self._ensure_index_data()
        index = self._index_df

        for symbol in pool:
            df = self._get_stock_data(symbol, days)
            if df.empty or len(df) < 100:
                continue

            # 注入指数收益列（供策略使用）
            if index is not None and not index.empty:
                idx_close_map = index.set_index("date")["close"]
                df["idx_close"] = df["date"].map(idx_close_map)
                df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
                df["index_return"] = df["idx_ret"].fillna(0.0)

            # 策略特殊列注入
            if self.config.enrich_func is not None:
                try:
                    df, param_override = self.config.enrich_func(df, symbol)
                except Exception as e:
                    logger.debug("enrich_func 失败 {}: {}", symbol, e)
                    continue

            try:
                strategy = strategy_class() if isinstance(strategy_class, type) else strategy_class
                signals = strategy.generate_signals(df.copy(), is_backtest=True)
            except Exception as e:
                logger.debug("策略 {} 在 {} 上运行失败: {}", self.config.strategy_name, symbol, e)
                continue

            if signals is None or signals.empty:
                continue

            # 只取买入信号
            direction_col = "direction" if "direction" in signals.columns else "signal"
            buy_mask = (
                signals[direction_col] == 1
                if direction_col == "signal"
                else signals[direction_col].str.lower() == "buy"
            )
            buy_idx = signals.index[buy_mask].tolist()

            for idx in buy_idx:
                sig_date = df.loc[idx, "date"]
                if isinstance(sig_date, pd.Timestamp):
                    sig_date = sig_date.to_pydatetime()
                all_events.append(
                    {
                        "symbol": symbol,
                        "date": pd.Timestamp(sig_date),
                        "entry": float(df.loc[idx, "close"]),
                        "_df_idx": idx,
                    }
                )

        if not all_events:
            logger.warning("策略 {} 在股票池中无买入信号", self.config.strategy_name)
            return self._empty_result()

        signals_df = pd.DataFrame(all_events)
        return self.run(signals_df)

    def run(self, signals_df: pd.DataFrame) -> StudyResult:
        """执行事件研究分析。

        Args:
            signals_df: 必须包含列 ['symbol', 'date', 'entry']
                - symbol: 股票代码
                - date: 信号日期 (pd.Timestamp)
                - entry: 入场价格
        """
        required = {"symbol", "date", "entry"}
        missing = required - set(signals_df.columns)
        if missing:
            raise ValueError(f"信号 DataFrame 缺少列: {missing}")

        df = signals_df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        # 获取指数数据
        self._ensure_index_data()

        # 计算每个信号的 forward returns
        events: list[StudyEvent] = []
        for _, row in df.iterrows():
            symbol = str(row["symbol"])
            sig_date = row["date"]
            entry_price = float(row["entry"])

            stock_df = self._get_stock_data(symbol)
            if stock_df.empty:
                continue

            # 定位信号在 K 线中的位置
            sig_idx = self._find_signal_index(stock_df, sig_date, entry_price)
            if sig_idx is None:
                continue

            # 确定信号所属时期
            sig_date_str = (
                sig_date.strftime("%Y-%m-%d") if hasattr(sig_date, "strftime") else str(sig_date)
            )
            period = self._classify_period(sig_date)
            year = pd.Timestamp(sig_date).year

            event = StudyEvent(
                symbol=symbol,
                date=sig_date_str,
                entry=entry_price,
                period=period,
                year=year,
            )

            # 计算各周期的 forward return
            for h in self.config.horizons:
                ret = self._forward_return(stock_df, sig_idx, h, entry_price)
                if ret is not None:
                    event.returns[str(h)] = ret

                    # 超额收益
                    excess = self._excess_return(sig_date, h, ret)
                    if excess is not None:
                        event.excess[str(h)] = excess

            if event.returns:  # 至少有一个 horizon 有数据
                events.append(event)

        if not events:
            return self._empty_result()

        # 集中度检查
        concentration_issues = self._check_concentration(events)

        # 按 horizon 分组统计
        per_horizon = self._aggregate_by_horizon(events)

        # 按年份分桶
        per_year: dict[int, list[HorizonResult]] = {}
        years = sorted({e.year for e in events})
        for y in years:
            year_events = [e for e in events if e.year == y]
            if year_events:
                per_year[y] = self._aggregate_by_horizon(year_events)

        # 按时期分桶
        per_period: dict[str, list[HorizonResult]] = {}
        for p in ("train", "val", "outside"):
            period_events = [e for e in events if e.period == p]
            if period_events:
                per_period[p] = self._aggregate_by_horizon(period_events)

        result = StudyResult(
            strategy_name=self.config.strategy_name,
            config=self.config,
            n_events=len(events),
            n_symbols=len({e.symbol for e in events}),
            concentration_issues=concentration_issues,
            per_horizon=per_horizon,
            per_year=per_year,
            per_period=per_period,
            events=events,
        )

        # 写入输出文件
        if self.config.output_path:
            self._save(result)

        return result

    # ── 内部数据方法 ─────────────────────────────────────

    def _ensure_index_data(self) -> None:
        if self._index_df is not None:
            return
        self._index_df = fetch_index_klines(self.config.index_code)
        if self._index_df.empty:
            logger.warning("指数 {} 数据获取失败，无法计算超额收益", self.config.index_code)

    def _get_stock_data(self, symbol: str, days: int = 600) -> pd.DataFrame:
        if symbol not in self._stock_cache:
            df = fetch_stock_klines(symbol, days)
            if not df.empty:
                self._stock_cache[symbol] = df
        return self._stock_cache.get(symbol, pd.DataFrame())

    @staticmethod
    def _find_signal_index(
        stock_df: pd.DataFrame, sig_date: pd.Timestamp, entry_price: float
    ) -> int | None:
        """在 K 线中定位信号日期对应的行索引。"""
        dates = stock_df["date"]
        # 精确匹配
        matches = dates[dates == sig_date].index.tolist()
        if matches:
            return matches[0]
        # 容错：找最近的前一个交易日
        before = dates[dates <= sig_date]
        if not before.empty:
            return before.index[-1]
        return None

    def _classify_period(self, sig_date: pd.Timestamp | datetime | str) -> str:
        """分类信号所属时期: train / val / outside。"""
        d = pd.Timestamp(sig_date)
        train_start_d = pd.Timestamp(self.config.train_start)
        train_end_d = pd.Timestamp(self.config.train_end)
        val_start_d = pd.Timestamp(self.config.val_start)
        val_end_d = pd.Timestamp(self.config.val_end)

        if d < train_start_d:
            return "outside"
        if d <= train_end_d:
            return "train"
        if val_start_d <= d <= val_end_d:
            return "val"
        return "outside"

    def _forward_return(
        self, stock_df: pd.DataFrame, idx: int, horizon: int, entry_price: float
    ) -> float | None:
        """计算信号后 horizon 个交易日的收益。"""
        target_idx = idx + horizon
        if target_idx >= len(stock_df):
            return None

        if self.config.entry_mode == "next_open" and idx + 1 < len(stock_df):
            effective_entry = float(stock_df.iloc[idx + 1]["open"])
        else:
            effective_entry = entry_price

        if effective_entry <= 0:
            return None

        exit_price = float(stock_df.iloc[target_idx]["close"])
        raw_return = (exit_price - effective_entry) / effective_entry
        # 扣除交易成本
        net_return = raw_return - self.config.cost_rate
        return round(net_return, 4)

    def _excess_return(
        self, sig_date: pd.Timestamp, horizon: int, strategy_return: float
    ) -> float | None:
        """计算超额收益（策略收益 - 指数同期收益）。"""
        if self._index_df is None or self._index_df.empty:
            return None

        idx_dates = self._index_df["date"]
        matches = idx_dates[idx_dates == sig_date].index.tolist()
        if not matches:
            return None

        idx_pos = matches[0]
        target_pos = idx_pos + horizon
        if target_pos >= len(self._index_df):
            return None

        index_entry = float(self._index_df.iloc[idx_pos]["close"])
        index_exit = float(self._index_df.iloc[target_pos]["close"])
        if index_entry <= 0:
            return None

        index_return = (index_exit - index_entry) / index_entry
        return round(strategy_return - index_return, 4)

    @staticmethod
    def _check_concentration(events: list[StudyEvent]) -> list[dict[str, Any]]:
        """检查单股信号集中度。"""
        from collections import Counter

        symbol_count = Counter(e.symbol for e in events)
        total = len(events)
        issues = []
        for sym, cnt in symbol_count.items():
            ratio = cnt / total
            if ratio > DEFAULT_CONCENTRATION_THRESHOLD:
                issues.append(
                    {
                        "symbol": sym,
                        "count": cnt,
                        "ratio": round(ratio, 4),
                        "action": "集中度过高，建议剔除后重验",
                    }
                )
        return issues

    # ── 聚合统计 ─────────────────────────────────────────

    def _aggregate_by_horizon(self, events: list[StudyEvent]) -> list[HorizonResult]:
        """按 horizon 聚合统计事件。"""
        results = []
        for h in self.config.horizons:
            h_str = str(h)
            excess_vals = [e.excess[h_str] for e in events if h_str in e.excess]
            ret_vals = [e.returns[h_str] for e in events if h_str in e.returns]

            if len(excess_vals) < self.config.min_samples:
                continue

            excess_arr = np.array(excess_vals, dtype=float)
            ret_arr = np.array(ret_vals, dtype=float)
            t_stat, p_value = welch_t(excess_arr)

            avg_excess = float(np.mean(excess_arr))
            std_excess = float(np.std(excess_arr, ddof=1)) if len(excess_arr) > 1 else 0.0
            avg_return = float(np.mean(ret_arr))
            win_rate = float(np.sum(excess_arr > 0) / len(excess_arr))
            sharpe = avg_return / std_excess if std_excess > 0 else 0.0
            excess_sharpe = avg_excess / std_excess if std_excess > 0 else 0.0

            # 检查 p_value 是否是 nan
            p = p_value if not (isinstance(p_value, float) and math.isnan(p_value)) else 1.0

            results.append(
                HorizonResult(
                    horizon=h,
                    n_events=len(excess_vals),
                    avg_return=round(avg_return, 4),
                    avg_excess=round(avg_excess, 4),
                    std_excess=round(std_excess, 4),
                    t_stat=round(t_stat, 4),
                    p_value=round(p, 4),
                    win_rate=round(win_rate, 4),
                    sharpe=round(sharpe, 4),
                    excess_sharpe=round(excess_sharpe, 4),
                    significant=p < 0.05,
                )
            )

        return results

    # ── 结果输出 ─────────────────────────────────────────

    def _save(self, result: StudyResult) -> None:
        """保存事件研究结果到 JSON。"""
        path = Path(self.config.output_path)  # type: ignore[arg-type]
        data = self._to_dict(result)
        atomic_write_json(path, data)
        logger.info("事件研究结果已写入: {}", path)

    @staticmethod
    def _to_dict(result: StudyResult) -> dict:
        """将结果序列化为可 JSON 序列化的字典。"""
        return {
            "strategy": result.strategy_name,
            "config": {
                "horizons": result.config.horizons,
                "index_code": result.config.index_code,
                "entry_mode": result.config.entry_mode,
                "cost_rate": result.config.cost_rate,
                "train_period": [result.config.train_start, result.config.train_end],
                "val_period": [result.config.val_start, result.config.val_end],
            },
            "summary": {
                "n_events": result.n_events,
                "n_symbols": result.n_symbols,
                "concentration_issues": result.concentration_issues,
            },
            "per_horizon": [
                {
                    "horizon": r.horizon,
                    "n_events": r.n_events,
                    "avg_return": r.avg_return,
                    "avg_excess": r.avg_excess,
                    "std_excess": r.std_excess,
                    "t_stat": r.t_stat,
                    "p_value": r.p_value,
                    "win_rate": r.win_rate,
                    "sharpe": r.sharpe,
                    "excess_sharpe": r.excess_sharpe,
                    "significant": r.significant,
                }
                for r in result.per_horizon
            ],
            "per_year": {
                str(year): [
                    {
                        "horizon": r.horizon,
                        "avg_excess": r.avg_excess,
                        "p_value": r.p_value,
                        "n_events": r.n_events,
                        "significant": r.significant,
                    }
                    for r in results
                ]
                for year, results in result.per_year.items()
            },
            "per_period": {
                period: [
                    {
                        "horizon": r.horizon,
                        "avg_excess": r.avg_excess,
                        "p_value": r.p_value,
                        "n_events": r.n_events,
                        "significant": r.significant,
                    }
                    for r in results
                ]
                for period, results in result.per_period.items()
            },
        }

    def _empty_result(self) -> StudyResult:
        return StudyResult(
            strategy_name=self.config.strategy_name,
            config=self.config,
            n_events=0,
            n_symbols=0,
            concentration_issues=[],
            per_horizon=[],
            per_year={},
            per_period={},
            events=[],
        )

    def print_summary(self, result: StudyResult) -> None:
        """打印易读的事件研究摘要。"""
        header = f"═ 事件研究: {result.strategy_name} ═"
        print()
        print("═" * len(header))
        print(header)
        print("═" * len(header))
        print(f"总事件: {result.n_events} | 股票数: {result.n_symbols}")
        print(f"入场: {self.config.entry_mode} | 成本: {self.config.cost_rate:.1%}")
        print(f"训练期: {self.config.train_start} ~ {self.config.train_end}")
        print(f"验证期: {self.config.val_start} ~ {self.config.val_end}")
        print()

        if result.concentration_issues:
            print("⚠ 集中度警告:")
            for issue in result.concentration_issues:
                print(
                    f"   {issue['symbol']}: {issue['ratio']:.0%} ({issue['count']}/{result.n_events})"
                )
            print()

        if not result.per_horizon:
            print("无有效事件")
            print()
            return

        print(
            f"{'维度':>6} {'事件数':>6} {'超额收益':>10} {'t统计':>8} {'p值':>8} {'胜率':>6} {'夏普':>8} {'显著':>6}"
        )
        print("-" * 65)
        for r in result.per_horizon:
            sig_mark = "✅" if r.significant else "❌"
            print(
                f"{r.horizon:>4}d {r.n_events:>6} {r.avg_excess:>+9.2%} "
                f"{r.t_stat:>+8.3f} {r.p_value:>8.4f} {r.win_rate:>5.0%} "
                f"{r.excess_sharpe:>8.2f} {sig_mark:>4}"
            )

        if result.per_year:
            print()
            print("年份分桶:")
            for year in sorted(result.per_year.keys()):
                yr_results = result.per_year[year]
                yr_str = " | ".join(
                    f"{r['horizon']}d: {r['avg_excess']:+>.1%}{'✅' if r['significant'] else '❌'}"
                    for r in yr_results
                )
                print(f"  {year}: {yr_str}")

        if result.per_period:
            print()
            print("时期分桶:")
            for period in ("train", "val", "outside"):
                if period not in result.per_period:
                    continue
                pd_results = result.per_period[period]
                pd_str = " | ".join(
                    f"{r['horizon']}d: {r['avg_excess']:+>.1%} (n={r['n_events']}){'✅' if r['significant'] else '❌'}"
                    for r in pd_results
                )
                print(f"  {period}: {pd_str}")

        print()


# 默认股票池（与 generic_event_study.py 一致）
_DEFAULT_POOL = [
    "002472",
    "300033",
    "002230",
    "603501",
    "300308",
    "300502",
    "600519",
    "300750",
    "002594",
    "300059",
    "002475",
    "600050",
    "000977",
    "688041",
    "002371",
]


def cli() -> None:
    """命令行入口: python -m gugu.analysis.event_study --strategy ..."""
    import argparse

    parser = argparse.ArgumentParser(description="统一事件研究框架")
    parser.add_argument(
        "--strategy", required=True, help="策略名（用于从 signals_history.jsonl 筛选）"
    )
    parser.add_argument("--history", default="data/signals_history.jsonl", help="信号历史路径")
    parser.add_argument("--output", default=None, help="结果写入路径")
    parser.add_argument("--entry-mode", default="next_open", choices=["close", "next_open"])
    parser.add_argument("--horizons", default="3,5,10", help="逗号分隔的周期")
    parser.add_argument("--index", default="000300", help="基准指数代码")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]

    config = StudyConfig(
        strategy_name=args.strategy,
        horizons=horizons,
        index_code=args.index,
        entry_mode=args.entry_mode,
        output_path=args.output,
    )
    study = EventStudy(config)
    result = study.run_from_history(args.history)
    study.print_summary(result)


if __name__ == "__main__":
    cli()
