"""筹码集中度过滤器 — 选股池准入过滤器（慢变量，季度更新）。

基于股东户数变化判断筹码集中/分散趋势：
- **避雷针**：户数激增 / 户均持股骤减 → 主力派发，散户接盘，拒绝买入
- **潜伏者**：户数连续下降 + 户均持股增加 → 筹码集中，主力收集，confidence_boost=+0.15

与 MoneyFlowFilter 的区别：
- 筹码是**慢变量**（季度披露），不应因数据缺失而拒绝整个信号
- 资金流是**快变量**（日频），数据缺失意味着无法确认主力方向，应拒绝

数据缺失处理：
- 季度数据未披露时默认放行（pass=True），reasons 标记"筹码数据不可用"
- 全市场约 20% 股票当季数据可能缺失（非报告期或延迟披露）
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from gugu.data.collectors.holder_structure import HolderStructureCollector
from gugu.utils.log import get_logger

logger = get_logger()


class HolderConcentrationFilter:
    """筹码集中度过滤器。

    利用 ``HolderStructureCollector`` 获取股东户数及户均持股数据，
    判断筹码是否在集中（潜伏者）或分散（避雷针），给出准入/否决结论。

    阈值（可通过 settings.yaml ``holder_concentration_filter`` 段覆盖）：
        quarterly_change_pct_threshold: 单季度户数增幅阈值%（避雷针信号1，默认 30.0）
        avg_hold_change_threshold:     户均持股环比变化阈值%（避雷针信号2，默认 -20.0）
        lurker_consecutive_quarters:   潜伏者连续下降季度数（默认 2）
        lurker_total_decline_threshold: 潜伏者累计降幅阈值%（默认 20.0）
        monthly_change_pct_threshold:  月度（单季度代理）户数变化阈值%（潜伏者信号2 代理，默认 15.0）
    """

    # 默认阈值（类常量，作为文档锚点和默认值）
    DEFAULT_QUARTERLY_CHANGE_PCT = 30.0
    DEFAULT_MONTHLY_CHANGE_PCT = 15.0
    DEFAULT_AVG_HOLD_CHANGE = -20.0
    DEFAULT_LURKER_CONSECUTIVE_QUARTERS = 2
    DEFAULT_LURKER_TOTAL_DECLINE = 20.0

    def __init__(self) -> None:
        # 当日缓存：{symbol: (date, result_dict)}
        self._cache: dict[str, tuple[date, dict[str, Any]]] = {}
        # 延迟初始化采集器（避免启动时即请求网络/初始化 httpx）
        self._collector: HolderStructureCollector | None = None

    def _get_collector(self) -> HolderStructureCollector:
        """延迟初始化采集器。"""
        if self._collector is None:
            self._collector = HolderStructureCollector()
        return self._collector

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
        """检查个股筹码结构是否通过过滤。

        避雷针信号（任一触发 → pass=False，拒绝买入）：
        1. 单季度户数增幅 > 30%（holder_num_pct > 30.0）
        2. 户均持股环比减少 > 20%（avg_hold_change < -20.0）
        3. 户数已连续 2 季度增长且累计 > 20%（需要历史数据）

        潜伏者信号（同时满足 → pass=True，confidence_boost=+0.15）：
        1. 连续 2 季度户数下降，累计降幅 > 20%
        2. 户均持股环比增加 > 15%（avg_hold_change > 15.0）

        数据缺失处理：
        - 季度数据未披露：默认放行（pass=True），reasons 标记"筹码数据不可用"
        - 筹码是慢变量，不应因数据缺失而拒绝整个信号

        Args:
            symbol: 股票代码，如 ``"600519"``。

        Returns:
            包含 pass / label / holder_num / holder_num_pct / avg_hold_change /
            end_date / reasons / data_missing 的字典。
        """
        # 1. 查缓存
        cached = self._get_cached(symbol)
        if cached is not None:
            return cached

        code = str(symbol).strip().zfill(6)

        # 2. 读取配置阈值（容错：配置缺失时用默认值）
        thresholds = self._load_thresholds()

        # 3. 数据缺失默认结果（筹码是慢变量，默认放行）
        default: dict[str, Any] = {
            "pass": True,
            "label": "数据缺失",
            "holder_num": 0.0,
            "holder_num_pct": 0.0,
            "avg_hold_change": 0.0,
            "end_date": "",
            "reasons": ["筹码数据不可用"],
            "data_missing": True,
        }

        # 4. 获取最新快照
        collector = self._get_collector()
        try:
            df = await collector.fetch_holder_structure(symbol=code)
        except Exception as e:
            logger.warning(f"筹码结构数据获取异常 {code}: {e}")
            self._set_cached(symbol, default)
            return default

        if df is None or df.empty:
            logger.debug(f"筹码结构数据为空 {code}，默认放行（慢变量）")
            self._set_cached(symbol, default)
            return default

        # 5. 提取最新一期数据
        try:
            row = df.iloc[0]
            holder_num = float(row.get("holder_num", 0) or 0)
            holder_num_pct = float(row.get("holder_num_pct", 0) or 0)
            avg_hold_change = float(row.get("avg_hold_change", 0) or 0)
            end_date = str(row.get("end_date", "") or "")
        except (IndexError, ValueError, TypeError) as e:
            logger.warning(f"提取筹码结构数据失败 {code}: {e}")
            self._set_cached(symbol, default)
            return default

        # 6. 获取历史数据（用于"连续 N 季度"判断）
        history_df: pd.DataFrame | None = None
        try:
            history_df = await collector.fetch_holder_history(
                code, quarters=thresholds["lurker_consecutive_quarters"] + 1
            )
        except Exception as e:
            logger.debug(f"筹码历史数据获取失败 {code}（不影响单季度信号）: {e}")

        # 7. 避雷针信号判定
        reasons: list[str] = []
        lightning_rod = False  # 避雷针触发标志

        # 信号1：单季度户数增幅 > 阈值
        if holder_num_pct > thresholds["quarterly_change_pct_threshold"]:
            lightning_rod = True
            reasons.append(
                f"单季度户数增幅 {holder_num_pct:.2f}% > "
                f"{thresholds['quarterly_change_pct_threshold']}%，主力派发嫌疑"
            )

        # 信号2：户均持股环比减少 > 阈值
        if avg_hold_change < thresholds["avg_hold_change_threshold"]:
            lightning_rod = True
            reasons.append(
                f"户均持股环比变化 {avg_hold_change:.2f}% < "
                f"{thresholds['avg_hold_change_threshold']}%，筹码分散"
            )

        # 信号3：连续 2 季度增长且累计 > 阈值（需要历史数据）
        if history_df is not None and len(history_df) >= thresholds["lurker_consecutive_quarters"]:
            consecutive_up, cumulative_pct = self._check_consecutive_trend(
                history_df, direction="up"
            )
            if (
                consecutive_up >= thresholds["lurker_consecutive_quarters"]
                and cumulative_pct > thresholds["lurker_total_decline_threshold"]
            ):
                lightning_rod = True
                reasons.append(
                    f"户数连续 {consecutive_up} 季度增长，累计增幅 "
                    f"{cumulative_pct:.2f}% > "
                    f"{thresholds['lurker_total_decline_threshold']}%，派发趋势确认"
                )

        if lightning_rod:
            result: dict[str, Any] = {
                "pass": False,
                "label": "避雷针",
                "holder_num": holder_num,
                "holder_num_pct": holder_num_pct,
                "avg_hold_change": avg_hold_change,
                "end_date": end_date,
                "reasons": reasons,
                "data_missing": False,
            }
            self._set_cached(symbol, result)
            return result

        # 8. 潜伏者信号判定
        lurker_signals = 0
        lurker_reasons: list[str] = []

        # 信号1：连续 N 季度户数下降，累计降幅 > 阈值
        if history_df is not None and len(history_df) >= thresholds["lurker_consecutive_quarters"]:
            consecutive_down, cumulative_decline = self._check_consecutive_trend(
                history_df, direction="down"
            )
            if (
                consecutive_down >= thresholds["lurker_consecutive_quarters"]
                and cumulative_decline > thresholds["lurker_total_decline_threshold"]
            ):
                lurker_signals += 1
                lurker_reasons.append(
                    f"户数连续 {consecutive_down} 季度下降，累计降幅 "
                    f"{cumulative_decline:.2f}% > "
                    f"{thresholds['lurker_total_decline_threshold']}%，筹码集中"
                )

        # 信号2：户均持股环比增加 > 阈值（用 monthly_change_pct_threshold 作为代理阈值）
        if avg_hold_change > thresholds["monthly_change_pct_threshold"]:
            lurker_signals += 1
            lurker_reasons.append(
                f"户均持股环比增加 {avg_hold_change:.2f}% > "
                f"{thresholds['monthly_change_pct_threshold']}%，主力收集"
            )

        if lurker_signals >= 2:
            result = {
                "pass": True,
                "label": "潜伏者",
                "holder_num": holder_num,
                "holder_num_pct": holder_num_pct,
                "avg_hold_change": avg_hold_change,
                "end_date": end_date,
                "reasons": lurker_reasons + ["confidence_boost=+0.15"],
                "data_missing": False,
            }
            self._set_cached(symbol, result)
            return result

        # 9. 中性：既非避雷针也非潜伏者
        result = {
            "pass": True,
            "label": "中性",
            "holder_num": holder_num,
            "holder_num_pct": holder_num_pct,
            "avg_hold_change": avg_hold_change,
            "end_date": end_date,
            "reasons": [
                f"户数变化 {holder_num_pct:.2f}%，户均持股变化 {avg_hold_change:.2f}%，"
                f"筹码结构无显著异常"
            ],
            "data_missing": False,
        }
        self._set_cached(symbol, result)
        return result

    @staticmethod
    def _check_consecutive_trend(
        history_df: pd.DataFrame, direction: str
    ) -> tuple[int, float]:
        """检查历史数据中连续同向变化的季度数及累计变化率。

        Args:
            history_df: 按 end_date 降序排列的历史数据（最近 N 期）。
            direction: ``"up"``（户数增长）或 ``"down"``（户数下降）。

        Returns:
            (连续季度数, 累计变化率%)
        """
        try:
            # history_df 按 end_date 降序，需反转为升序计算累计变化
            sorted_df = history_df.sort_values("end_date", ascending=True)
            pcts = sorted_df["holder_num_pct"].tolist()

            consecutive = 0
            if direction == "up":
                for p in reversed(pcts):
                    if p > 0:
                        consecutive += 1
                    else:
                        break
            else:  # down
                for p in reversed(pcts):
                    if p < 0:
                        consecutive += 1
                    else:
                        break

            # P1-2 修复：累计变化率使用复利公式 (1+p1)*(1+p2)*...-1，
            # 原代码 sum(pcts) 是简单求和，数学错误（30%+30% 应为 69% 而非 60%）
            if consecutive > 0:
                recent_pcts = list(reversed(pcts))[:consecutive]
                cumulative_ratio = 1.0
                for p in recent_pcts:
                    cumulative_ratio *= (1.0 + p / 100.0)
                cumulative = (cumulative_ratio - 1.0) * 100.0
                # down 方向返回降幅绝对值（正数），与调用方 > threshold 比较一致
                if direction == "down":
                    cumulative = abs(cumulative)
            else:
                cumulative = 0.0

            return consecutive, round(cumulative, 2)
        except Exception as e:
            logger.warning(f"连续趋势计算失败: {e}")
            return 0, 0.0

    def _load_thresholds(self) -> dict[str, Any]:
        """从 settings.yaml 加载阈值，配置缺失时用默认值。

        直接读取原始 YAML（绕过 pydantic AppConfig，因为
        ``holder_concentration_filter`` 段未在 AppConfig 中定义）。
        """
        defaults: dict[str, Any] = {
            "quarterly_change_pct_threshold": self.DEFAULT_QUARTERLY_CHANGE_PCT,
            "monthly_change_pct_threshold": self.DEFAULT_MONTHLY_CHANGE_PCT,
            "avg_hold_change_threshold": self.DEFAULT_AVG_HOLD_CHANGE,
            "lurker_consecutive_quarters": self.DEFAULT_LURKER_CONSECUTIVE_QUARTERS,
            "lurker_total_decline_threshold": self.DEFAULT_LURKER_TOTAL_DECLINE,
        }
        try:
            from gugu._legacy_config import load_yaml

            raw = load_yaml("settings")
            section = raw.get("holder_concentration_filter", {})
            if not isinstance(section, dict):
                section = {}
        except Exception:
            section = {}

        for key in defaults:
            if key in section:
                try:
                    if key == "lurker_consecutive_quarters":
                        defaults[key] = int(section[key])
                    else:
                        defaults[key] = float(section[key])
                except (ValueError, TypeError):
                    pass

        return defaults

    def describe(self) -> dict[str, Any]:
        """返回过滤器描述（用于飞书看板/调试）。"""
        return {
            "name": "筹码集中度过滤器",
            "quarterly_change_threshold": self.DEFAULT_QUARTERLY_CHANGE_PCT,
            "monthly_change_threshold": self.DEFAULT_MONTHLY_CHANGE_PCT,
            "avg_hold_change_threshold": self.DEFAULT_AVG_HOLD_CHANGE,
            "lurker_consecutive_quarters": self.DEFAULT_LURKER_CONSECUTIVE_QUARTERS,
            "lurker_total_decline_threshold": self.DEFAULT_LURKER_TOTAL_DECLINE,
            "positioning": "选股池准入过滤器（慢变量，季度更新）",
        }
