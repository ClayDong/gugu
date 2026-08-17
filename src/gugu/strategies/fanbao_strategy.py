"""小圈圈儿《反包策略》V2.0——策略类。

对应《反包策略》广东经济出版社，2025年6月
原书完整修正版，覆盖 5+1 种战法：
1. 跌停板战法（极端反包）
2. 炸板反包（新手首选）
3. 一字涨停板反包
4. 反包回调战法
5. 跳空涨停式反包
+ 补充：暴力洗盘战法

核心改进（对照原书 V2.0 框架）：
- 进场信号统一为「高开」——非当日判断，非滞后信号
- 增加 6 条风险红线检查
- 增加 7 阶段情绪周期判断
- 增加性价比评分
- 增加通用前置条件（位置/题材/市值）
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.strategies.base import Strategy
from gugu.strategies.fanbao.rules import (
    check_red_lines,
    detect_all,
    detect_sentiment_cycle,
)
from gugu.utils.log import get_logger

logger = get_logger()


class FanbaoStrategy(Strategy):
    """小圈圈儿《反包策略》V2.0 完整版。

    覆盖 5+1 种反包战法，统一进场信号为「次日高开」。
    """

    name = "fanbao"
    category = "short_term"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._hs300_regime: pd.DataFrame | None = None

    def validate_params(self) -> None:
        mv_min = float(self.params.get("mv_min", 50))
        if mv_min < 0:
            raise ValueError("mv_min must be >= 0")
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 0.05~0.11 之间")

    def _load_hs300_regime(self) -> pd.DataFrame | None:
        """加载 HS300 数据用于情绪周期判断。"""
        if self._hs300_regime is not None:
            return self._hs300_regime
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[3]
        cache = project_root / "data" / "parquet_cache" / "hs300_500d.parquet"
        if not cache.exists():
            return None
        try:
            hs = pd.read_parquet(cache)
            hs["date"] = pd.to_datetime(hs["date"])
            hs = hs.sort_values("date").reset_index(drop=True)
            self._hs300_regime = hs
            return hs
        except Exception:
            return None

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成信号。

        V2.0 改进：
        - 进场信号统一为「次日高开」——原书核心指令
        - 信号日在"断板/炸板/跌停"日发出买入信号，但说明次日高开才进场
        - 也可只在高开日才发信号（由 use_gate_open 参数控制）

        参数:
            use_gate_open (bool): True=只有高开日才发信号；False=断板日发信号+备注高开进场
        """
        result = df.copy()
        self._ensure_columns(result)
        result["signal"] = 0
        result["confidence"] = 0.0
        result["signal_detail"] = ""
        result["strategy_name"] = ""

        n = len(result)
        if n < 60:
            return result

        # V2.0 前视修复：创建 mask df，最后一行 close/high/low 用前一日值替换
        df_masked = df.copy()
        df_masked.loc[df.index[-1], "close"] = df["close"].iloc[-2]
        df_masked.loc[df.index[-1], "high"] = df["high"].iloc[-2]
        df_masked.loc[df.index[-1], "low"] = df["low"].iloc[-2]

        # 获取代码
        code = "unknown"
        if "code" in result.columns and result["code"].notna().any():
            code = str(result["code"].iloc[-1])
        else:
            code = getattr(self, "_code", "unknown")

        # 获取流通市值
        float_mv: float | None = None
        if "float_mv" in result.columns:
            mv_series = result["float_mv"]
            if bool(mv_series.notna().any()):
                float_mv = float(mv_series.iloc[-1])

        # 获取名称（用于ST检查）
        name = None
        if "name" in result.columns and result["name"].notna().any():
            name = str(result["name"].iloc[-1])

        # 参数
        use_gate_open = bool(self.params.get("use_gate_open", True))
        is_hot_topic = bool(self.params.get("is_hot_topic", False))
        skip_red_lines = bool(self.params.get("skip_red_lines", False))

        # 红线检查（用遮盖后的 df_masked，不含今日 close 前视）
        if not skip_red_lines:
            close_price = float(df_masked["close"].iloc[-1]) if len(df_masked) else None
            red = check_red_lines(
                code, df_masked, name=name, float_mv=float_mv, close_price=close_price
            )
            if not red.passed:
                if self.params.get("verbose", False):
                    logger.info(f"fanbao [{code}] 红线阻挡: {red.reasons}")
                return result

        # 情绪周期判断
        skip_sentiment = bool(self.params.get("skip_sentiment", False))
        if not skip_sentiment:
            hs300 = self._load_hs300_regime()
            sentiment = detect_sentiment_cycle(market_df=hs300)
            if not sentiment["is_operatable"]:
                if self.params.get("verbose", False):
                    logger.info(f"fanbao [{code}] 情绪周期不适合: {sentiment['stage']}")
                return result

        # 运行全部战法检测
        # V2.0 前视修复：df_masked 已在上方创建，最后一行 close/high/low 用前日值替换
        signals = detect_all(
            code,
            df_masked,
            float_mv=float_mv,
            is_hot_topic=is_hot_topic,
            skip_red_lines=True,  # 上面已检查
            sentiment_ok=True,  # 上面已检查
            skip_checks=bool(self.params.get("skip_checks", False)),
            today_open=float(df["open"].iloc[-1]),
            today_prev_close=float(df["close"].iloc[-2]),
        )

        if not signals:
            return result

        # 取评分最高的信号
        best = signals[0]

        # V2.0 核心：只有高开日才发 signal=1
        # 断板日/炸板日/跌停日不发买入信号（原书：当天不能潜伏）
        last = n - 1
        open_pct = result["open"].iloc[last] / result["close"].iloc[last - 1] - 1

        if use_gate_open and open_pct <= 0:
            # 没有高开，不发信号，但记录观察信息
            result.loc[last, "signal_detail"] = (
                f"V2.0观察-{best.strategy}: {best.reason}（未高开，等待）"
            )
            return result

        # 高开（或 use_gate_open=False）：发买入信号
        result.loc[last, "signal"] = 1
        result.loc[last, "confidence"] = min(0.95, best.confidence)
        result.loc[last, "signal_detail"] = (
            f"V2.0-{best.strategy}: {best.reason} "
            f"[性价比{best.score:.0f}分/置信度{best.confidence:.0%}]"
        )
        result.loc[last, "strategy_name"] = best.strategy

        # 记录 meta（可选）
        if best.meta:
            for k, v in best.meta.items():
                result.loc[last, f"fanbao_{k}"] = str(v) if not isinstance(v, (int, float)) else v

        return result
