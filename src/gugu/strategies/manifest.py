"""策略注册中心 — 统一加载、查询和管理所有策略资产。

从 ``config/strategy_manifest.yaml`` 加载策略清单，提供统一查询接口。

用法：
    from gugu.strategies.manifest import StrategyManifest

    manifest = StrategyManifest()
    # 查询所有已启用策略
    enabled = manifest.list_enabled()
    # 按类型查询
    signal_strategies = manifest.list_by_category("signal_strategies")
    # 按出处查询
    chen_strategies = manifest.list_by_source("炒股的智慧")
    # 获取策略参数
    params = manifest.get_params("trailing_stop")
    # 生成策略看板文本（供飞书通知）
    dashboard = manifest.format_dashboard()
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

_MANIFEST_PATH: Path = PROJECT_ROOT / "config" / "strategy_manifest.yaml"


class StrategyManifest:
    """策略注册中心。

    从 ``strategy_manifest.yaml`` 加载策略清单，提供统一查询接口。
    支持：
    - 按类别查询（signal/risk/take_profit/skill/dynamic）
    - 按启用状态查询
    - 按出处查询
    - 获取策略参数
    - 生成策略看板文本（供飞书通知）
    """

    def __init__(self, manifest_path: Path | None = None) -> None:
        self._path = manifest_path or _MANIFEST_PATH
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """加载策略清单。"""
        if not self._path.exists():
            logger.warning(f"策略清单文件不存在: {self._path}")
            return
        try:
            self._data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            total = sum(len(v) for v in self._data.values() if isinstance(v, dict))
            logger.info(f"[manifest] 加载 {total} 个策略")
        except Exception as e:
            logger.error(f"[manifest] 策略清单加载失败: {e}")

    # ========== 查询接口 ==========

    def list_all(self) -> dict[str, dict[str, Any]]:
        """列出所有策略（按类别分组）。"""
        return self._data

    def list_by_category(self, category: str) -> dict[str, Any]:
        """按类别查询策略。

        Args:
            category: 类别名，可选值：
                signal_strategies / risk_strategies / take_profit_strategies
                / skill_strategies / dynamic_strategies
        """
        return self._data.get(category, {})

    def list_enabled(self) -> list[dict[str, Any]]:
        """列出所有已启用的策略（跨类别）。"""
        result: list[dict[str, Any]] = []
        for category, strategies in self._data.items():
            if not isinstance(strategies, dict):
                continue
            for key, info in strategies.items():
                if not isinstance(info, dict):
                    continue
                if info.get("enabled", False):
                    result.append(
                        {
                            "key": key,
                            "category": category,
                            "name": info.get("name", key),
                            "source": info.get("source", ""),
                            "summary": info.get("summary", ""),
                            "validation": info.get("validation", ""),
                        }
                    )
        return result

    def list_disabled(self) -> list[dict[str, Any]]:
        """列出所有未启用的策略。"""
        result: list[dict[str, Any]] = []
        for category, strategies in self._data.items():
            if not isinstance(strategies, dict):
                continue
            for key, info in strategies.items():
                if not isinstance(info, dict):
                    continue
                if not info.get("enabled", False):
                    result.append(
                        {
                            "key": key,
                            "category": category,
                            "name": info.get("name", key),
                            "source": info.get("source", ""),
                            "summary": info.get("summary", ""),
                            "reason": info.get("notes", info.get("validation", "")),
                        }
                    )
        return result

    def list_by_source(self, source_keyword: str) -> list[dict[str, Any]]:
        """按出处关键词查询策略。"""
        result: list[dict[str, Any]] = []
        for category, strategies in self._data.items():
            if not isinstance(strategies, dict):
                continue
            for key, info in strategies.items():
                if not isinstance(info, dict):
                    continue
                if source_keyword in info.get("source", ""):
                    result.append(
                        {
                            "key": key,
                            "category": category,
                            "name": info.get("name", key),
                            "source": info.get("source", ""),
                            "summary": info.get("summary", ""),
                        }
                    )
        return result

    def get_strategy(self, key: str) -> dict[str, Any] | None:
        """获取单个策略的完整信息。"""
        for strategies in self._data.values():
            if isinstance(strategies, dict) and key in strategies:
                return strategies[key]
        return None

    def get_params(self, key: str) -> dict[str, Any]:
        """获取策略参数（从 manifest 的 params 字段）。"""
        info = self.get_strategy(key)
        if info and isinstance(info, dict):
            return info.get("params", {})
        return {}

    # ========== 统计 ==========

    def stats(self) -> dict[str, int]:
        """策略统计。"""
        total = 0
        enabled = 0
        disabled = 0
        by_category: dict[str, int] = {}
        for category, strategies in self._data.items():
            if not isinstance(strategies, dict):
                continue
            count = len(strategies)
            by_category[category] = count
            total += count
            for info in strategies.values():
                if isinstance(info, dict) and info.get("enabled", False):
                    enabled += 1
                else:
                    disabled += 1
        return {
            "total": total,
            "enabled": enabled,
            "disabled": disabled,
            "by_category": by_category,
        }

    # ========== 看板格式化 ==========

    def format_dashboard(self) -> str:
        """生成策略看板文本（供飞书通知）。

        输出格式为 Markdown 文本，可直接用于飞书卡片。
        """
        s = self.stats()
        lines = [
            "**策略注册中心 · 全景看板**",
            "",
            f"总计 **{s['total']}** 个策略 | "
            f"启用 🟢 **{s['enabled']}** | "
            f"停用 ⚪ **{s['disabled']}**",
            "",
        ]

        # 类别统计（与 strategy_manifest.yaml 的顶层 key 对齐）
        category_names = {
            "signal_strategies": "信号生成策略",
            "risk_strategies": "风控/择时/止盈策略",
            "skill_strategies": "Skill 策略",
            "dynamic_strategies": "动态策略生成",
        }
        for cat_key, cat_name in category_names.items():
            strategies = self._data.get(cat_key, {})
            if not isinstance(strategies, dict) or not strategies:
                continue
            enabled_count = sum(
                1
                for info in strategies.values()
                if isinstance(info, dict) and info.get("enabled", False)
            )
            total_count = len(strategies)
            lines.append(f"### {cat_name}（{enabled_count}/{total_count}）")
            lines.append("")
            for key, info in strategies.items():
                if not isinstance(info, dict):
                    continue
                name = info.get("name", key)
                source = info.get("source", "")
                summary = info.get("summary", "")
                is_enabled = info.get("enabled", False)
                status_icon = "🟢" if is_enabled else "⚪"
                lines.append(f"{status_icon} **{name}** — {summary}")
                lines.append(f"   出处：{source}")
            lines.append("")

        lines.append("---")
        lines.append("出处：策略注册中心 · gugu · 策略治理")

        return "\n".join(lines)

    def format_compact(self) -> str:
        """生成精简版看板（仅列出名称和状态）。"""
        s = self.stats()
        lines = [
            f"**策略注册中心**（{s['enabled']}/{s['total']} 启用）",
            "",
        ]
        for category, strategies in self._data.items():
            if not isinstance(strategies, dict) or not strategies:
                continue
            cat_name = {
                "signal_strategies": "信号",
                "risk_strategies": "风控/择时/止盈",
                "skill_strategies": "Skill",
                "dynamic_strategies": "动态",
            }.get(category, category)
            enabled_list = [
                info.get("name", key)
                for key, info in strategies.items()
                if isinstance(info, dict) and info.get("enabled", False)
            ]
            if enabled_list:
                lines.append(f"**{cat_name}**：{'、'.join(enabled_list)}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_manifest() -> StrategyManifest:
    """获取策略清单单例。"""
    return StrategyManifest()
