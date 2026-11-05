"""多重检验校正契约：防止挑最好窗口被当成 alpha。"""

from __future__ import annotations

import pytest

from gugu.validation.multiple_testing import (
    benjamini_hochberg,
    bonferroni,
    report_correction,
)


def test_bonferroni_tightens_threshold() -> None:
    result = bonferroni([0.01, 0.04, 0.2])
    assert result["n_tests"] == 3
    assert result["adjusted_alpha"] == pytest.approx(0.05 / 3)
    assert result["rejected"] == [True, False, False]


def test_naive_vs_corrected_shows_the_gap() -> None:
    report = report_correction([0.01, 0.04, 0.2])
    assert report["naive_significant"] == 2
    assert report["bonferroni"]["n_significant"] == 1


def test_benjamini_hochberg_step_up() -> None:
    result = benjamini_hochberg([0.01, 0.04, 0.2])
    assert result["n_significant"] == 1
    assert result["rejected"] == [True, False, False]


def test_bh_can_be_less_conservative_than_bonferroni() -> None:
    p_values = [0.01, 0.02, 0.03, 0.04]
    assert benjamini_hochberg(p_values)["n_significant"] >= bonferroni(p_values)["n_significant"]


def test_empty_input() -> None:
    assert bonferroni([])["rejected"] == []
    assert benjamini_hochberg([])["n_significant"] == 0


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        bonferroni([1.5])
    with pytest.raises(ValueError):
        bonferroni([0.01], alpha=0.0)
