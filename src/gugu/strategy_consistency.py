"""gugu 策略一致性校验模块。

P0-4 修复：启动时自动校验 strategy_defaults.yaml / strategy_manifest.yaml / settings.yaml 三层一致性。
"""

from __future__ import annotations

from typing import Any

from gugu.config import get_config, strategy_defaults
from gugu.utils.log import get_logger

logger = get_logger()


def validate_strategy_consistency() -> list[str]:
    """校验策略三层一致性，返回问题列表（空列表表示一致）。

    校验规则：
    1. settings 中启用的策略必须在 registry 中注册
    2. registry 中的策略必须有默认参数
    3. settings 启用策略与 manifest enabled 状态一致
    4. verified + experimental 列表不重叠
    5. 权重总和 = 1.0（仅 verified 策略）
    """
    issues: list[str] = []

    try:
        from gugu.strategies.registry import list_strategies as _list_strategies

        # 获取三层数据
        defaults_keys = set(strategy_defaults().keys())
        registry_keys = set(_list_strategies())
        config = get_config()

        # 1. settings 启用的策略必须在 registry 中
        enabled_keys: set[str] = set()
        verified = getattr(config.strategy, "verified_strategies", None)
        experimental = getattr(config.strategy, "experimental_strategies", None)

        if verified:
            enabled_keys.update(verified)
        if experimental:
            enabled_keys.update(experimental)
        if not enabled_keys:
            # 向后兼容：使用原 enabled 列表
            enabled_keys = set(getattr(config.strategy, "enabled", []))

        missing_in_registry = enabled_keys - registry_keys
        if missing_in_registry:
            issues.append(
                f"策略在 settings 中启用但未在 registry 注册: {sorted(missing_in_registry)}"
            )

        # 2. registry 中的策略应有默认参数（排除 risk/skill 类策略）
        risk_skill_prefixes = ("stage_", "danger_", "no_average_", "trailing_", "regime_")
        signal_strategy_keys = {k for k in registry_keys if not k.startswith(risk_skill_prefixes)}
        missing_in_defaults = signal_strategy_keys - defaults_keys
        # 排除已知不需要默认参数的策略（如 ml_factor, dynamic_generator）
        no_defaults_ok = {"ml_factor", "dynamic_generator", "leopard_three_factors"}
        missing_in_defaults -= no_defaults_ok
        if missing_in_defaults:
            issues.append(f"策略已注册但无默认参数: {sorted(missing_in_defaults)}")

        # 3. verified 和 experimental 不重叠
        if verified and experimental:
            overlap = set(verified) & set(experimental)
            if overlap:
                issues.append(f"策略同时出现在 verified 和 experimental: {sorted(overlap)}")

        # 4. 权重校验
        weights = getattr(config.strategy, "weights", {})
        if weights:
            weight_sum = sum(weights.values())
            if abs(weight_sum - 1.0) > 0.01:
                issues.append(f"verified 策略权重总和 {weight_sum:.2f} ≠ 1.0")

        # 6. experimental 策略不在 enabled_by_regime 的任意 regime 中时记录 warning（非 error）
        try:
            enabled_by_regime = getattr(config.strategy, "enabled_by_regime", None)
            if experimental and enabled_by_regime:
                all_regime_strategies: set[str] = set()
                for regime_strategies in enabled_by_regime.values():
                    if isinstance(regime_strategies, list):
                        all_regime_strategies.update(regime_strategies)
                for exp_strat in experimental:
                    if exp_strat not in all_regime_strategies:
                        logger.warning(
                            "[策略一致性] experimental 策略 {} 不在 enabled_by_regime 的任意 regime 中（预期行为，仅 paper 模式使用）",
                            exp_strat,
                        )
        except Exception as e:
            logger.debug(f"experimental 策略 regime 检查跳过: {e}")

        # 5. manifest 一致性（轻量级检查，manifest 可能不存在）
        try:
            from pathlib import Path

            import yaml

            manifest_path = Path("config/strategy_manifest.yaml")
            if manifest_path.exists():
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                signal_strategies = manifest.get("signal_strategies", {})
                for name in enabled_keys:
                    m_entry = signal_strategies.get(name)
                    if m_entry and not m_entry.get("enabled", False):
                        issues.append(f"策略 {name} 在 settings 启用但 manifest 禁用")
        except Exception as e:
            logger.debug(f"manifest 一致性检查跳过: {e}")

    except Exception as e:
        logger.warning(f"策略一致性校验异常: {e}")
        issues.append(f"校验过程异常: {e}")

    # 输出结果
    if issues:
        for issue in issues:
            logger.warning("[策略一致性] {}", issue)
    else:
        logger.info("[策略一致性] ✅ 三层一致，无问题")

    return issues


# v0.7.5 P0-9修复：策略验证状态检查（experimental → verified 升级建议）
# v0.8.1 升级：5 项解锁条件（新增交易次数下限，防止小样本过拟合）
# 依据：pullback_low 教训——30股小样本通过→1710股大样本失败（过拟合）；
# box_breakout 教训——样本外 Sharpe 勉强>0 但实盘无信号。
# 门槛说明：Sharpe>0 太弱（box_breakout 实测 -1.43），但为保持兼容保留 0 阈值；
# 真正的防线是 min_total_trades ≥ 30（样本量不足直接不通过）。
_UNLOCK_CONDITIONS = {
    "out_of_sample_sharpe": {"threshold": 0.0, "description": "样本外 Sharpe > 0"},
    "param_stability": {"threshold": 0.80, "description": "参数稳定性 ≥ 80%（v0.8.1 从 50% 上调）"},
    "max_drawdown": {"threshold": 0.25, "description": "最大回撤 < 25%"},
    "avg_return": {"threshold": 0.0, "description": "平均收益 ≥ 0"},
    "min_total_trades": {"threshold": 30, "description": "样本外交易次数 ≥ 30（v0.8.1 新增）"},
}


def check_strategy_upgrade_eligibility(
    strategy: str,
    walk_forward_results: dict[str, float] | None = None,
) -> tuple[bool, list[str]]:
    """检查实验性策略是否满足升级到 verified 的条件。

    Args:
        strategy: 策略名称
        walk_forward_results: Walk-Forward 验证结果，包含：
            - out_of_sample_sharpe: 样本外夏普比率
            - param_stability: 参数稳定性（0-1）
            - max_drawdown: 最大回撤（正数，如 0.15 表示 15%）
            - avg_return: 平均收益
            - min_total_trades: 样本外总交易次数（v0.8.1 新增，缺省视为 0）

    Returns:
        (可升级, 未通过的条件列表)
    """
    if walk_forward_results is None:
        walk_forward_results = {}

    failed: list[str] = []
    # v0.8.1：交易次数缺失时从 total_trades 字段兼容读取
    results = dict(walk_forward_results)
    if "min_total_trades" not in results and "total_trades" in results:
        results["min_total_trades"] = float(results.get("total_trades", 0) or 0)

    for condition, cfg in _UNLOCK_CONDITIONS.items():
        value = results.get(condition)
        if value is None:
            failed.append(f"{condition}: 无数据")
            continue
        threshold = cfg["threshold"]
        if condition == "max_drawdown":
            # 回撤是越小越好
            if value >= threshold:
                failed.append(f"{cfg['description']}（实际 {value:.2%}）")
        else:
            # 其他是越大越好
            if value <= threshold:
                failed.append(f"{cfg['description']}（实际 {value:.4f}）")

    return len(failed) == 0, failed


def suggest_strategy_upgrades(
    validation_results: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """批量检查所有实验性策略的升级资格。

    Args:
        validation_results: {策略名: {指标名: 值}}

    Returns:
        {策略名: {"eligible": bool, "failed": list, "results": dict}}
    """
    if validation_results is None:
        validation_results = {}

    suggestions: dict[str, dict[str, Any]] = {}
    for strategy, results in validation_results.items():
        eligible, failed = check_strategy_upgrade_eligibility(strategy, results)
        suggestions[strategy] = {
            "eligible": eligible,
            "failed": failed,
            "results": results,
        }
        if eligible:
            logger.info(
                "[P0-9] 策略 {} ✅ 满足升级条件，建议从 experimental 升级到 verified", strategy
            )
        else:
            logger.info(f"[P0-9] 策略 {strategy} ❌ 不满足升级条件: {failed}")

    return suggestions
