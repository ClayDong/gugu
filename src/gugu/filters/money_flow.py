"""资金流因子过滤：基于主力净流入判断资金方向。"""

from __future__ import annotations

from datetime import date
from typing import Any

from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()


class MoneyFlowFilter:
    """资金流因子过滤器。

    利用 DataManager.fetch_stock_flow 获取个股资金流数据，
    判断近期主力资金是否在流入，给出评分与通过/否决结论。
    """

    def __init__(self) -> None:
        # 当日缓存：{symbol: (date, result_dict)}
        self._cache: dict[str, tuple[date, dict[str, Any]]] = {}

    def _get_cached(self, symbol: str) -> dict[str, Any] | None:
        """获取当日缓存，跨日自动失效。"""
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        cached_date, result = entry
        if cached_date != date.today():
            # 跨日失效
            del self._cache[symbol]
            return None
        return result

    def _set_cached(self, symbol: str, result: dict[str, Any]) -> None:
        """写入当日缓存。"""
        self._cache[symbol] = (date.today(), result)

    async def check(self, symbol: str) -> dict[str, Any]:
        """检查个股资金流是否通过过滤。

        P2-4 修复（炒股专家审查）：
        - 数据缺失时默认拒绝（pass=False）而非放行
        - 原因：资金流跟随的本质 = 趋势确认 + 资金进入
          "不知道有没有主力流入就买入" = 盲目进场
        - 仅 signal_only 模式下放行（仅观察，不下单）

        Args:
            symbol: 股票代码，如 "600519"。

        Returns:
            包含 pass / main_net_today / main_pct_today / main_net_5d / score / reasons / data_missing 的字典。
        """
        # 1. 查缓存
        cached = self._get_cached(symbol)
        if cached is not None:
            return cached

        # 2. 默认结果（P2-4 修复：数据缺失时默认拒绝）
        # 原 logic: pass=True（避免拦截信号）
        # 新 logic: pass=False（无资金流数据 → 无法确认主力方向 → 拒绝）
        # 仅 signal_only 模式下放行（仅观察）
        try:
            from gugu.config import get_config
            exec_mode = get_config().execution.mode
        except Exception:
            exec_mode = "paper"  # 默认 paper 模式（严格）

        # signal_only 模式仅观察，放行；paper/live 模式必须严格拒绝
        pass_when_missing = exec_mode == "signal_only"

        default: dict[str, Any] = {
            "pass": pass_when_missing,
            "main_net_today": 0.0,
            "main_pct_today": 0.0,
            "main_net_5d": 0.0,
            "score": 0.0,
            "reasons": ["资金流数据不可用，无法确认主力方向"] if not pass_when_missing else [],
            "data_missing": True,
        }

        # 3. 获取资金流数据
        try:
            dm = data_manager()
            df = await dm.fetch_stock_flow(symbol)
        except Exception as e:
            logger.warning(f"资金流数据获取异常 {symbol}: {e}")
            self._set_cached(symbol, default)
            return default

        if df is None or df.empty:
            logger.warning(f"资金流数据为空 {symbol}")
            self._set_cached(symbol, default)
            return default

        # 4. 提取今日数据
        main_net_today = 0.0
        main_pct_today = 0.0

        # 优先从 date 列定位最新日
        if "date" in df.columns:
            try:
                latest_row = df.iloc[-1]
                main_net_today = float(latest_row.get("main_net", 0) or 0)
                main_pct_today = float(latest_row.get("main_pct", 0) or 0)
            except (IndexError, ValueError, TypeError) as e:
                logger.warning(f"提取今日资金流失败 {symbol}: {e}")
        else:
            # 无 date 列时取最后一行
            try:
                latest_row = df.iloc[-1]
                main_net_today = float(latest_row.get("main_net", 0) or 0)
                main_pct_today = float(latest_row.get("main_pct", 0) or 0)
            except (IndexError, ValueError, TypeError) as e:
                logger.warning(f"提取今日资金流失败 {symbol}: {e}")

        # 5. 近5日主力净流入合计
        main_net_5d = 0.0
        try:
            # 取最近5行（按日期升序排列，末尾为最新）
            tail = df.tail(5)
            main_net_5d = float(tail["main_net"].sum())
        except Exception as e:
            logger.warning(f"计算近5日主力净流入失败 {symbol}: {e}")

        # 6. 评分计算
        # score = clamp(main_net_5d / abs(main_net_5d).max() * 0.5 + main_pct_today / 10 * 0.5, 0, 1)
        # abs(main_net_5d).max() 用近5日绝对值最大值作为归一化基准
        try:
            tail = df.tail(5)
            abs_max = float(tail["main_net"].abs().max())
            if abs_max == 0:
                normalized_5d = 0.0
            else:
                normalized_5d = main_net_5d / abs_max
        except Exception:
            normalized_5d = 0.0

        raw_score = normalized_5d * 0.5 + (main_pct_today / 10) * 0.5
        score = max(0.0, min(1.0, raw_score))

        # 7. 过滤规则判定
        reasons: list[str] = []

        # 规则1：近5日主力净流入合计 > 0
        if main_net_5d > 0:
            reasons.append(f"近5日主力净流入合计 {main_net_5d / 1e8:.2f}亿 > 0，资金在流入")
        else:
            reasons.append(f"近5日主力净流入合计 {main_net_5d / 1e8:.2f}亿 <= 0，资金在流出")

        # 规则2：今日主力净流入占比 > -5%
        if main_pct_today > -5.0:
            reasons.append(f"今日主力净流入占比 {main_pct_today:.2f}% > -5%，未大幅流出")
        else:
            reasons.append(f"今日主力净流入占比 {main_pct_today:.2f}% <= -5%，大幅流出")

        passed = main_net_5d > 0 and main_pct_today > -5.0

        result: dict[str, Any] = {
            "pass": passed,
            "main_net_today": main_net_today,
            "main_pct_today": main_pct_today,
            "main_net_5d": main_net_5d,
            "score": score,
            "reasons": reasons,
        }

        self._set_cached(symbol, result)
        return result
