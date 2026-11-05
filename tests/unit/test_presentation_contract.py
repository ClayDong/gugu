"""呈现层契约测试（领域约定锁）。

背景：2026-10-20 用户发现两处呈现层缺陷逃过了多专家 review：
1. Web UI 红涨绿跌颠倒（实现了美股约定，A 股应为红涨绿跌）
2. API 返回 market_value 但持仓详情表无市值列

根因：领域约定没有单一事实源，review 只审代码逻辑不审领域映射。
本测试把 docs/DOMAIN_CONVENTIONS.md 的约定固化为机器断言，任何回归直接失败。

约定依据：docs/DOMAIN_CONVENTIONS.md（修改颜色/emoji 前必须先改约定文档与测试）。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"

# A 股方向色（红涨绿跌）
RED = "#f85149"
GREEN = "#3fb950"


def _read(relpath: str) -> str:
    return (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


class TestWebUiColorContract:
    """Web 仪表盘方向色契约：红涨绿跌。"""

    def test_positive_is_red_not_green(self) -> None:
        """涨/盈利 = 红 #f85149，禁止绿色。"""
        html = _read("src/gugu/web/static/index.html")
        assert f".card .value.positive{{color:{RED}}}" in html
        # 确认没有把 positive 写成绿色
        assert ".card .value.positive{color:#3fb950}" not in html

    def test_negative_is_green_not_red(self) -> None:
        """跌/亏损 = 绿 #3fb950，禁止红色。"""
        html = _read("src/gugu/web/static/index.html")
        assert f".card .value.negative{{color:{GREEN}}}" in html
        assert ".card .value.negative{color:#f85149}" not in html

    def test_buy_direction_is_red(self) -> None:
        """买入方向 = 红（A 股约定，主动买盘红色）。"""
        html = _read("src/gugu/web/static/index.html")
        assert f".signal-dir-buy{{color:{RED}" in html

    def test_sell_direction_is_green(self) -> None:
        """卖出方向 = 绿。"""
        html = _read("src/gugu/web/static/index.html")
        assert f".signal-dir-sell{{color:{GREEN}" in html

    def test_trades_table_buy_is_red(self) -> None:
        """交易记录表行内买卖颜色与 CSS 约定一致（买入红卖出绿）。"""
        html = _read("src/gugu/web/static/index.html")
        # 买入分支取 RED、卖出分支取 GREEN
        assert f"t.direction==='buy'?'{RED}':'{GREEN}'" in html

    def test_positions_table_has_market_value_column(self) -> None:
        """呈现完整性：持仓详情表必须有市值列（API 已返回 market_value）。"""
        html = _read("src/gugu/web/static/index.html")
        assert "<th>市值</th>" in html
        # 市值单元格渲染 market_value 字段
        assert "p.market_value" in html


class TestFundFlowEmojiContract:
    """资金流 emoji 契约：流入=🔴，流出=🟢（全仓库统一）。"""

    def test_flow_formatter_inflow_is_red(self) -> None:
        """flow_formatter 净流入用🔴。"""
        code = _read("src/gugu/notifier/flow_formatter.py")
        assert "🔴 +{f:.1f}亿" in code

    def test_intraday_monitor_no_corrupted_chars(self) -> None:
        """intraday_monitor 无 U+FFFD 损坏字符（4 字节 UTF-8 编辑损坏防护）。"""
        code = _read("src/gugu/engine/intraday_monitor.py")
        assert "\ufffd" not in code, "检测到 U+FFFD：emoji 被非 UTF-8 工具损坏"

    def test_intraday_monitor_inflow_red_outflow_green(self) -> None:
        """intraday_monitor 所有流入文案用🔴、流出文案用🟢，方向与颜色一一对应。"""
        code = _read("src/gugu/engine/intraday_monitor.py")
        for line in code.splitlines():
            if "message=" not in line or ("🔴" not in line and "🟢" not in line):
                continue
            if "流入" in line:
                assert "🔴" in line, f"流入文案必须用🔴: {line.strip()}"
                assert "🟢" not in line
            elif "流出" in line:
                assert "🟢" in line, f"流出文案必须用🟢: {line.strip()}"
                assert "🔴" not in line


class TestSourceEncodingIntegrity:
    """全仓库编码完整性：任何源码文件不得出现 U+FFFD 替换符。

    根因防护：外部编辑器以非 UTF-8/UTF-16 中转读写会把 4 字节 emoji
    （🔴🟢 等）损坏为 U+FFFD，肉眼难察觉，直接进入生产通知文案。
    """

    def test_no_replacement_char_in_source(self) -> None:
        bad_files: list[str] = []
        for py in SRC_DIR.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            if "\ufffd" in text:
                bad_files.append(str(py.relative_to(PROJECT_ROOT)))
        assert bad_files == [], f"以下源码文件含 U+FFFD 损坏字符: {bad_files}"

    def test_all_source_files_valid_utf8(self) -> None:
        """所有源码必须是合法 UTF-8（含 HTML/静态资源）。"""
        targets = list(SRC_DIR.rglob("*.py")) + list(SRC_DIR.rglob("*.html"))
        for f in targets:
            try:
                f.read_bytes().decode("utf-8")
            except UnicodeDecodeError as e:
                raise AssertionError(f"{f} 不是合法 UTF-8: {e}")
