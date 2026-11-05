"""策略代码指纹测试（2026-11-05）。

指纹是「证据绑定代码版本」的基石，所以这里锁定的是它的**诚实性**：
- 解析不到源码必须返回 None，不能编一个固定值冒充；
- 参数变化必须改变指纹；
- 同一份代码同一套参数必须稳定（否则误伤正常准入）。
"""

from __future__ import annotations

import pytest

from gugu.validation.code_fingerprint import (
    declared_params,
    module_source_digest,
    strategy_fingerprint,
)

MODULE = "gugu.strategies.two_b_reversal"


def test_module_digest_is_stable() -> None:
    first = module_source_digest(MODULE)
    second = module_source_digest(MODULE)
    assert first and first == second
    assert len(first) == 64


def test_module_digest_differs_across_modules() -> None:
    assert module_source_digest(MODULE) != module_source_digest("gugu.strategies.double_yin")


@pytest.mark.parametrize("name", ["", "   ", "gugu.not_a_real_module_xyz", "no_such_top_level_pkg"])
def test_module_digest_returns_none_when_unresolvable(name: str) -> None:
    assert module_source_digest(name) is None


def test_fingerprint_is_stable() -> None:
    first = strategy_fingerprint(MODULE, params={"cycle_days": 14})
    second = strategy_fingerprint(MODULE, params={"cycle_days": 14})
    assert first and first == second


def test_fingerprint_ignores_param_key_order() -> None:
    a = strategy_fingerprint(MODULE, params={"a": 1, "b": 2})
    b = strategy_fingerprint(MODULE, params={"b": 2, "a": 1})
    assert a == b


def test_fingerprint_changes_with_params() -> None:
    """参数改了结果就变了，指纹必须跟着变，否则旧证据会继续有效。"""
    base = strategy_fingerprint(MODULE, params={"cycle_days": 14})
    changed = strategy_fingerprint(MODULE, params={"cycle_days": 10})
    assert base != changed


def test_fingerprint_without_params_uses_empty_set() -> None:
    assert strategy_fingerprint(MODULE) == strategy_fingerprint(MODULE, params={})


def test_fingerprint_returns_none_for_unresolvable_module() -> None:
    assert strategy_fingerprint("gugu.not_a_real_module_xyz") is None


def test_fingerprint_differs_across_modules() -> None:
    assert strategy_fingerprint(MODULE) != strategy_fingerprint("gugu.strategies.double_yin")


def test_declared_params_reads_real_config() -> None:
    params = declared_params("box_breakout")
    assert isinstance(params, dict) and params
    assert "box_window" in params


def test_declared_params_returns_empty_for_unknown() -> None:
    assert declared_params("no_such_strategy_xyz") == {}
    assert declared_params("") == {}
