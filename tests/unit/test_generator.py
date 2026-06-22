"""策略生成器测试。"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import httpx
import pytest

from gugu.strategies import generator
from gugu.strategies.generator import (
    extract_class_name,
    extract_strategy_name,
    generate_strategy_code,
    save_strategy,
    validate_strategy_code,
)

VALID_CODE = '''\
"""测试策略。"""
from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class TestDemoStrategy(Strategy):
    name = "test_demo"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()
        df["signal"] = 0
        df["confidence"] = 0.5
        return df
'''


def test_extract_class_name() -> None:
    assert extract_class_name(VALID_CODE) == "TestDemoStrategy"


def test_extract_class_name_missing() -> None:
    with pytest.raises(ValueError, match="策略类名"):
        extract_class_name("class Foo:\n    pass")


def test_extract_strategy_name() -> None:
    assert extract_strategy_name(VALID_CODE) == "test_demo"


def test_extract_strategy_name_default() -> None:
    assert extract_strategy_name("class Foo:\n    pass") == "custom"


def test_validate_strategy_code_ok() -> None:
    validate_strategy_code(VALID_CODE)


def test_validate_strategy_code_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        validate_strategy_code("class Foo(\n")


def test_validate_strategy_code_no_strategy_class() -> None:
    code = "class Foo:\n    pass"
    with pytest.raises(ValueError, match="未找到继承自 Strategy"):
        validate_strategy_code(code)


def test_validate_strategy_code_multiple_strategy_classes() -> None:
    code = VALID_CODE.replace(
        "class TestDemoStrategy(Strategy):",
        "class A(Strategy):\n    name = \"a\"\n    def generate_signals(self, df): return df\n\nclass TestDemoStrategy(Strategy):",
    )
    with pytest.raises(ValueError, match="只能有一个 Strategy 子类"):
        validate_strategy_code(code)


def test_validate_strategy_code_missing_name() -> None:
    code = VALID_CODE.replace('    name = "test_demo"', "")
    with pytest.raises(ValueError, match="name 类属性"):
        validate_strategy_code(code)


def test_validate_strategy_code_missing_generate_signals() -> None:
    code = VALID_CODE.replace(
        "    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:\n        self._ensure_columns(df)\n        df = df.copy()\n        df[\"signal\"] = 0\n        df[\"confidence\"] = 0.5\n        return df\n",
        "",
    )
    with pytest.raises(ValueError, match="generate_signals"):
        validate_strategy_code(code)


def test_validate_strategy_code_bad_signature() -> None:
    code = VALID_CODE.replace(
        "def generate_signals(self, df: pd.DataFrame)",
        "def generate_signals(self, data: pd.DataFrame)",
    )
    with pytest.raises(ValueError, match="generate_signals"):
        validate_strategy_code(code)


def test_validate_strategy_code_forbidden_import() -> None:
    code = VALID_CODE.replace(
        "import pandas as pd",
        "import pandas as pd\nimport os",
    )
    with pytest.raises(ValueError, match="不允许"):
        validate_strategy_code(code)


def test_generate_strategy_code_no_api_key() -> None:
    with mock.patch.object(generator, "env") as mock_env:
        mock_env.return_value.llm_api_key = ""
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            generate_strategy_code("买入均线突破")


def test_generate_strategy_code_http_error() -> None:
    with (
        mock.patch.object(generator, "env") as mock_env,
        mock.patch("gugu.strategies.generator.httpx.post") as mock_post,
    ):
        mock_env.return_value.llm_api_key = "sk-test"
        mock_env.return_value.llm_base_url = "https://api.example.com"
        mock_env.return_value.llm_model = "test-model"
        mock_resp = mock.Mock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=mock.Mock(),
            response=mock_resp,
        )
        mock_post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="LLM API"):
            generate_strategy_code("买入均线突破")


def test_generate_strategy_code_malformed_response() -> None:
    with (
        mock.patch.object(generator, "env") as mock_env,
        mock.patch("gugu.strategies.generator.httpx.post") as mock_post,
    ):
        mock_env.return_value.llm_api_key = "sk-test"
        mock_env.return_value.llm_base_url = "https://api.example.com"
        mock_env.return_value.llm_model = "test-model"
        mock_resp = mock.Mock()
        mock_resp.json.return_value = {}
        mock_resp.text = "{}"
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="格式异常"):
            generate_strategy_code("买入均线突破")


def test_save_strategy(tmp_path: Path) -> None:
    registry = tmp_path / "registry.py"
    registry.write_text(
        "_REGISTRY: dict[str, type] = {\n    \"existing\": object,\n}\n",
        encoding="utf-8",
    )
    with (
        mock.patch.object(generator, "STRATEGIES_DIR", tmp_path),
        mock.patch.object(generator, "REGISTRY_FILE", registry),
    ):
        file_path = save_strategy(VALID_CODE)
        assert file_path.exists()
        assert "TestDemoStrategy" in file_path.read_text(encoding="utf-8")
        registry_content = registry.read_text(encoding="utf-8")
        assert "TestDemoStrategy" in registry_content
        assert "test_demo" in registry_content
