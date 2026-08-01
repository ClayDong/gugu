"""尾盘选股参数管理单元测试。"""

from __future__ import annotations

import json

import pytest

from gugu.screener.params import ScreenerParams


class TestScreenerParamsDefaults:
    """默认参数与基础行为。"""

    def test_default_values(self):
        p = ScreenerParams()
        # v0.7.9 优化后的默认阈值
        assert p.min_chg_pct == -1.5
        assert p.max_chg_pct == 2.0
        assert p.max_vol_ratio == 1.2
        assert p.min_turnover == 2.0
        assert p.max_turnover == 15.0
        assert p.max_candidates == 10
        assert p.auto_tune_enabled is True
        assert p.tune_lookback_days == 10

    def test_effective_max_candidates_by_regime(self):
        p = ScreenerParams(max_candidates=10)
        assert p.effective_max_candidates("bull") == 10
        assert p.effective_max_candidates("rally") == 10
        assert p.effective_max_candidates("sideways") == 6
        assert p.effective_max_candidates("bear") == 0
        assert p.effective_max_candidates("crash") == 0

    def test_should_skip_bear_crash(self):
        p = ScreenerParams()
        assert p.should_skip("bear") is True
        assert p.should_skip("crash") is True
        assert p.should_skip("bull") is False
        assert p.should_skip("sideways") is False

    def test_to_dict_contains_all_public_fields(self):
        p = ScreenerParams()
        d = p.to_dict()
        assert "min_chg_pct" in d
        assert "max_vol_ratio" in d
        assert "_BOUNDS" not in d
        assert d["auto_tune_enabled"] is True


class TestScreenerParamsAdjustment:
    """自动调优增量应用与边界裁剪。"""

    def test_apply_adjustment_updates_values(self):
        p = ScreenerParams(max_vol_ratio=1.0, min_turnover=3.0, min_chg_pct=-2.0)
        new_p = p.apply_adjustment(
            {"max_vol_ratio": -0.05, "min_turnover": 0.5, "min_chg_pct": -0.2}
        )
        assert new_p.max_vol_ratio == pytest.approx(0.95)
        assert new_p.min_turnover == pytest.approx(3.5)
        assert new_p.min_chg_pct == pytest.approx(-2.2)

    def test_apply_adjustment_clips_to_lower_bound(self):
        p = ScreenerParams(max_vol_ratio=0.52)
        new_p = p.apply_adjustment({"max_vol_ratio": -0.1})
        assert new_p.max_vol_ratio == pytest.approx(0.5)

    def test_apply_adjustment_clips_to_upper_bound(self):
        p = ScreenerParams(max_vol_ratio=1.48)
        new_p = p.apply_adjustment({"max_vol_ratio": 0.1})
        assert new_p.max_vol_ratio == pytest.approx(1.5)

    def test_apply_adjustment_ignores_unknown_keys(self):
        p = ScreenerParams()
        new_p = p.apply_adjustment({"unknown_key": 100.0})
        assert new_p == p

    def test_original_instance_unchanged(self):
        p = ScreenerParams(max_vol_ratio=1.0)
        new_p = p.apply_adjustment({"max_vol_ratio": -0.05})
        assert p.max_vol_ratio == 1.0
        assert new_p.max_vol_ratio == pytest.approx(0.95)


class TestScreenerParamsLoadSave:
    """参数持久化。"""

    def test_save_and_load_roundtrip(self, tmp_path):
        file_path = tmp_path / "screener_params.json"
        p = ScreenerParams(max_vol_ratio=0.85, min_turnover=4.0)
        p.save(file_path)

        loaded = ScreenerParams.load(file_path)
        assert loaded.max_vol_ratio == pytest.approx(0.85)
        assert loaded.min_turnover == pytest.approx(4.0)
        assert loaded.auto_tune_enabled is True

    def test_load_returns_defaults_when_file_missing(self, tmp_path):
        file_path = tmp_path / "not_exists.json"
        loaded = ScreenerParams.load(file_path)
        assert loaded.max_vol_ratio == pytest.approx(1.2)

    def test_load_ignores_invalid_fields(self, tmp_path):
        file_path = tmp_path / "screener_params.json"
        file_path.write_text(
            json.dumps(
                {"max_vol_ratio": 0.8, "invalid_field": 999, "_BOUNDS": {}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = ScreenerParams.load(file_path)
        assert loaded.max_vol_ratio == pytest.approx(0.8)
        assert not hasattr(loaded, "invalid_field")

    def test_load_returns_defaults_on_corrupted_file(self, tmp_path):
        file_path = tmp_path / "corrupt.json"
        file_path.write_text("not json", encoding="utf-8")
        loaded = ScreenerParams.load(file_path)
        assert loaded.max_vol_ratio == pytest.approx(1.2)
