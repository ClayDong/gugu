"""QMT（迅投）实盘 broker。

阶段四启用。当前为骨架，方法未实现。
接入时需安装 xtquant（QMT Python SDK）。
"""
from __future__ import annotations

from gugu.config import env
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()


class QmtBroker(BaseBroker):
    """QMT 实盘 broker（骨架，阶段四实现）。

    接入步骤：
    1. 安装 xtquant：pip install xtquant
    2. 启动 QMT 客户端并登录
    3. 配置 .env: QMT_PATH, QMT_ACCOUNT_ID, QMT_PASSWORD
    4. 实现 connect() / order() / get_position() 等方法
    """

    def __init__(self) -> None:
        self._account_id = env().qmt_account_id
        self._connected = False
        if not self._account_id:
            logger.warning("QMT 未配置 account_id，实盘功能不可用")

    def connect(self) -> bool:
        """连接 QMT 客户端（阶段四实现）。"""
        try:
            # from xtquant import xttrader, xtdata
            # self._trader = xttrader.XtQuantTrader(env().qmt_path, self._account_id)
            # self._trader.start()
            # self._connected = self._trader.connect()
            logger.warning("QMT 连接未实现（阶段四）")
            return False
        except Exception as e:
            logger.error(f"QMT 连接失败: {e}")
            return False

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """下单（阶段四实现）。

        安全机制：
        - 下单前人工确认（飞书通知 + 等待确认）
        - API 权限最小化（只交易，不转账）
        - IP 白名单
        """
        logger.warning("QMT 下单未实现（阶段四）")
        return OrderResult(
            False, symbol, direction, 0, 0, 0, message="QMT 实盘未启用（阶段四）"
        )

    def get_position(self, symbol: str) -> Position | None:
        logger.warning("QMT 查询持仓未实现（阶段四）")
        return None

    def get_portfolio(self) -> dict[str, Position]:
        logger.warning("QMT 查询持仓未实现（阶段四）")
        return {}

    def get_account(self) -> AccountInfo:
        logger.warning("QMT 查询账户未实现（阶段四）")
        return AccountInfo(cash=0, total_value=0, positions={})

    def update_price(self, symbol: str, price: float) -> None:
        """实盘自动更新，无需手动调用。"""
        pass

    def emergency_close_all(self) -> None:
        """一键平仓应急脚本（阶段四实现）。"""
        logger.warning("QMT 一键平仓未实现（阶段四）")
