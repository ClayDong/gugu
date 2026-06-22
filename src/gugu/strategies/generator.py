"""自然语言策略固化入口。

用户用自然语言描述策略，调用 LLM 生成符合 gugu Strategy 规范的 Python 代码。
代码写入 strategies/ 目录并自动注册。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import httpx

from gugu.config import env
from gugu.utils.log import get_logger

logger = get_logger()

STRATEGIES_DIR = Path(__file__).resolve().parent
REGISTRY_FILE = STRATEGIES_DIR / "registry.py"

# 生成代码中不允许出现的危险模块/属性
_FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "requests",
    "urllib",
    "socket",
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
}

# 危险属性访问黑名单
_FORBIDDEN_ATTRS = {
    "__builtins__",
    "__import__",
    "__class__",
    "__subclasses__",
    "__globals__",
    "__code__",
    "__bases__",
    "__mro__",
}


STRATEGY_TEMPLATE = '''\
"""{description}"""
from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class {class_name}(Strategy):
    """{description}"""

    name = "{strategy_name}"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

{logic_code}

        return df
'''


SYSTEM_PROMPT = """你是 gugu 交易系统的策略生成专家。请根据用户的自然语言描述，生成一个继承自 `Strategy` 的策略类。

要求：
1. 输出必须是纯 Python 代码，可直接写入 .py 文件。
2. 类名用 PascalCase，格式为 `<Name>Strategy`。
3. 必须设置 `name` 类属性（小写下划线）。
4. `generate_signals` 接收 DataFrame[date, open, high, low, close, volume, amount]，返回增加 `signal` 列（1=买, -1=卖, 0=持有）和 `confidence` 列（0-1）的 DataFrame。
5. 参数从 `self.params` 读取，参数名用英文小写下划线。
6. 使用 pandas/vectorized 操作，不要循环。
7. 买入/卖出条件用 `.shift(1)` 避免前视偏差。
8. 只输出代码本身，不要 markdown 代码块、解释或测试。

输出示例格式：

```python
from gugu.strategies.base import Strategy
import pandas as pd

class DemoStrategy(Strategy):
    name = "demo"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["signal"] = 0
        df.loc[(df["close"] > df["ma5"]) & (df["close"].shift(1) <= df["ma5"].shift(1)), "signal"] = 1
        df["confidence"] = (df["close"] / df["ma20"] - 1).abs().clip(0, 1)
        return df
```
"""


def generate_strategy_code(description: str, strategy_name: str | None = None) -> str:
    """调用 LLM 生成策略代码。

    Args:
        description: 自然语言策略描述
        strategy_name: 期望的策略名（小写下划线），不指定则 LLM 自拟

    Returns:
        Python 代码字符串
    """
    cfg = env()
    if not cfg.llm_api_key:
        raise RuntimeError("未配置 LLM_API_KEY，无法生成策略")

    prompt = f"策略描述：{description}\n"
    if strategy_name:
        prompt += f"策略名（请使用）: {strategy_name}\n"

    try:
        resp = httpx.post(
            f"{cfg.llm_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 1500,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"LLM API 返回错误: {exc.response.status_code} {exc.response.text}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"LLM API 请求失败: {exc}") from exc

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 返回格式异常: {resp.text[:500]}") from exc

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM 返回内容为空")

    # 去除 markdown 代码块
    code = re.sub(r"```python\n?", "", content)
    code = re.sub(r"```\n?", "", code)
    code = code.strip()
    if not code:
        raise RuntimeError("从 LLM 输出中提取不到代码")
    return code


def extract_class_name(code: str) -> str:
    """从代码中提取类名。"""
    match = re.search(r"class\s+(\w+)Strategy\s*\(", code)
    if not match:
        raise ValueError("生成代码中未找到符合 PascalCase 的策略类名")
    return match.group(1) + "Strategy"


def extract_strategy_name(code: str) -> str:
    """从代码中提取 name 属性。"""
    match = re.search(r'\s+name\s*=\s*["\']([a-z_]+)["\']', code)
    if match:
        return match.group(1)
    return "custom"


def validate_strategy_code(code: str) -> None:
    """校验生成的策略代码是否符合 gugu 规范。

    校验项：
    1. 可被 ast 解析（语法正确）。
    2. 存在且仅存在一个继承自 Strategy 的类。
    3. 类设置了 ``name`` 属性（字符串常量）。
    4. 类实现了 ``generate_signals(self, df)`` 方法。
    5. 未引入危险模块或函数（如 os/sys/subprocess/eval/exec/open 等）。

    Args:
        code: Python 代码字符串。

    Raises:
        ValueError: 代码不符合规范。
        SyntaxError: 代码存在语法错误。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SyntaxError(f"生成的策略代码存在语法错误: {exc}") from exc

    # 收集导入名和调用名，用于危险函数检测
    imported_names: set[str] = set()
    called_names: set[str] = set()
    strategy_classes: list[ast.ClassDef] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.split(".")[0])
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (isinstance(base, ast.Name) and base.id == "Strategy") or (
                    isinstance(base, ast.Attribute) and base.attr == "Strategy"
                ):
                    strategy_classes.append(node)

    dangerous = (imported_names | called_names) & _FORBIDDEN_IMPORTS
    if dangerous:
        raise ValueError(f"生成的代码包含不允许的模块/函数: {sorted(dangerous)}")

    # 检查危险属性访问（如 getattr(__builtins__, ...)）
    accessed_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            accessed_attrs.add(node.attr)
    attr_dangerous = accessed_attrs & _FORBIDDEN_ATTRS
    if attr_dangerous:
        raise ValueError(f"生成的代码包含不允许的属性访问: {sorted(attr_dangerous)}")

    if not strategy_classes:
        raise ValueError("生成的代码中未找到继承自 Strategy 的类")

    if len(strategy_classes) > 1:
        names = [cls.name for cls in strategy_classes]
        raise ValueError(f"生成的代码中只能有一个 Strategy 子类，发现 {len(names)} 个: {names}")

    cls = strategy_classes[0]

    # 检查 name 属性
    has_name = False
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "name":
                    if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        has_name = True
                    else:
                        raise ValueError("策略类的 name 必须是字符串常量")
    if not has_name:
        raise ValueError("策略类缺少 name 类属性")

    # 检查 generate_signals 方法
    gen_method = None
    for stmt in cls.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "generate_signals":
            gen_method = stmt
            break
    if gen_method is None:
        raise ValueError("策略类缺少 generate_signals 方法")

    args = gen_method.args
    if len(args.args) < 2 or args.args[0].arg != "self" or args.args[1].arg != "df":
        raise ValueError("generate_signals 方法签名必须是 generate_signals(self, df)")


def generate_and_save(description: str, strategy_name: str | None = None) -> Path:
    """自然语言描述 -> 生成代码 -> 校验 -> 保存 -> 注册。

    Args:
        description: 自然语言策略描述
        strategy_name: 期望的策略名（小写下划线），不指定则 LLM 自拟

    Returns:
        写入的文件路径
    """
    code = generate_strategy_code(description, strategy_name=strategy_name)
    validate_strategy_code(code)
    return save_strategy(code, strategy_name=strategy_name)


def save_strategy(code: str, strategy_name: str | None = None) -> Path:
    """保存策略代码并注册到 registry。

    Args:
        code: Python 代码
        strategy_name: 策略名，不指定则从代码提取

    Returns:
        写入的文件路径
    """
    if not strategy_name:
        strategy_name = extract_strategy_name(code)

    module_name = f"custom_{strategy_name}"
    file_path = STRATEGIES_DIR / f"{module_name}.py"

    if file_path.exists():
        logger.warning(f"策略文件已存在，覆盖: {file_path}")

    file_path.write_text(code, encoding="utf-8")
    logger.info(f"策略已保存: {file_path}")

    _register_strategy(strategy_name, module_name, extract_class_name(code))
    return file_path


def _register_strategy(strategy_name: str, module_name: str, class_name: str) -> None:
    """在 registry.py 中注册策略。"""
    content = REGISTRY_FILE.read_text(encoding="utf-8")

    import_line = f"from gugu.strategies.{module_name} import {class_name}\n"
    if import_line not in content:
        # 插入到 _REGISTRY 定义之前
        registry_idx = content.find("_REGISTRY: dict")
        if registry_idx == -1:
            raise RuntimeError("registry.py 中未找到 _REGISTRY 定义")
        content = content[:registry_idx] + import_line + content[registry_idx:]

    registry_entry = f'    "{strategy_name}": {class_name},\n'
    if registry_entry not in content:
        # 插入到 _REGISTRY 最后一个条目之后，大括号之前
        # 找到 _REGISTRY 定义后的第一个单独一行的 "}"
        registry_start = content.find("_REGISTRY: dict")
        closing_idx = content.find("\n}", registry_start)
        if closing_idx == -1:
            raise RuntimeError("registry.py 中未找到 _REGISTRY 的结束大括号")
        closing_idx += 1  # 指向 "}" 本身
        content = content[:closing_idx] + registry_entry + content[closing_idx:]

    REGISTRY_FILE.write_text(content, encoding="utf-8")
    logger.info(f"策略已注册: {strategy_name} -> {class_name}")
