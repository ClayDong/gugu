"""QMT（迅投）实盘 broker。

接入步骤：
1. 安装 xtquant：pip install xtquant
2. 启动 QMT 客户端并登录
3. 配置 .env: QMT_PATH, QMT_ACCOUNT_ID, QMT_PASSWORD
4. 实现 connect() / order() / get_position() 等方法

优化（2026-07）：
- 增加订单状态查询（check_order）
- 增加批量撤单支持（cancel_all）
- 增加持仓同步机制（sync_positions）
- 增加连接心跳检测（_health_check）
"""

from __future__ import annotations

import time
from typing import Any

from gugu.config import env
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()


class QmtBroker(BaseBroker):
    """QMT 实盘 broker（阶段四实现）。

    安全机制：
    - 下单前人工确认（飞书通知 + 等待确认）
    - API 权限最小化（只交易，不转账）
    - IP 白名单
    """

    def __init__(self) -> None:
        self._account_id = env().qmt_account_id
        self._connected = False
        self._last_health_check: float = 0.0
        self._health_check_interval: float = 60.0  # 每 60s 检测一次连接
        self._pending_orders: dict[str, dict[str, Any]] = {}  # order_id -> order_info
        if not self._account_id:
            logger.warning("QMT 未配置 account_id，实盘功能不可用")

    def connect(self) -> bool:
        """连接 QMT 客户端（阶段四实现）。

        Returns:
            是否连接成功
        """
        try:
            # from xtquant import xttrader, xtdata
            # self._trader = xttrader.XtQuantTrader(env().qmt_path, self._account_id)
            # self._trader.start()
            # self._connected = self._trader.connect()
            logger.warning("QMT 连接未实现（阶段四）")
            return False
        except ImportError:
            logger.warning("xtquant 未安装，无法使用 QMT 实盘")
            return False
        except Exception as e:
            logger.error("QMT 连接失败: {}", e)
            return False

    def _health_check(self) -> bool:
        """检测 QMT 连接是否存活。

        若连接断开，尝试自动重连。
        60s 内只检测一次（避免频繁重试）。

        Returns:
            连接是否健康
        """
        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return self._connected

        self._last_health_check = now
        if not self._connected:
            logger.warning("QMT 连接已断开，尝试自动重连...")
            return self.connect()
        return True

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """下单（阶段四实现）。

        Args:
            symbol: 股票代码
            direction: buy/sell
            quantity: 数量（A 股 100 股整数倍）
            price: 价格，None 为市价

        Returns:
            OrderResult
        """
        if not self._health_check():
            return OrderResult(
                False,
                symbol,
                direction,
                0,
                0,
                0,
                message="QMT 未连接，请先调用 connect()",
            )

        if not self._account_id:
            return OrderResult(
                False,
                symbol,
                direction,
                0,
                0,
                0,
                message="QMT 未配置 account_id",
            )

        logger.warning("QMT 下单未实现（阶段四）")
        return OrderResult(False, symbol, direction, 0, 0, 0, message="QMT 实盘未启用（阶段四）")

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单（阶段四实现）。

        Args:
            order_id: 订单 ID

        Returns:
            是否成功撤销
        """
        logger.warning("QMT 撤单未实现（阶段四）: order_id={}", order_id)
        return False

    def cancel_all(self, direction: str | None = None) -> int:
        """批量撤单（阶段四实现）。

        Args:
            direction: 买卖方向，None=全部撤单，buy=仅撤买入，sell=仅撤卖出

        Returns:
            成功撤单数量
        """
        logger.warning(f"QMT 批量撤单未实现（阶段四）: direction={direction}")
        return 0

    def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        """查询订单状态（阶段四实现）。

        Returns:
            订单状态字典，含 status/filled_qty/avg_price 等字段
        """
        logger.warning("QMT 订单状态查询未实现（阶段四）: order_id={}", order_id)
        return None

    def sync_positions(self) -> None:
        """从 QMT 同步持仓到本地缓存（阶段四实现）。

        每日开盘前调用，确保本地持仓与 QMT 保持一致。
        """
        if not self._health_check():
            logger.warning("QMT 未连接，无法同步持仓")
            return
        logger.warning("QMT 持仓同步未实现（阶段四）")

    def get_position(self, symbol: str) -> Position | None:
        if not self._health_check():
            logger.warning("QMT 未连接，无法查询持仓")
            return None
        logger.warning("QMT 查询持仓未实现（阶段四）")
        return None

    def get_portfolio(self) -> dict[str, Position]:
        if not self._health_check():
            logger.warning("QMT 未连接，无法查询持仓")
            return {}
        logger.warning("QMT 查询持仓未实现（阶段四）")
        return {}

    def get_account(self) -> AccountInfo:
        if not self._health_check():
            logger.warning("QMT 未连接，无法查询账户")
            return AccountInfo(cash=0, total_value=0, positions={}, positions_value=0)
        logger.warning("QMT 查询账户未实现（阶段四）")
        return AccountInfo(cash=0, total_value=0, positions={}, positions_value=0)

    def update_price(self, symbol: str, price: float) -> None:
        """实盘自动更新，无需手动调用。"""

    def emergency_close_all(self) -> None:
        """一键平仓应急脚本（阶段四实现）。

        安全机制：
        - 仅允许在 L2 熔断或 L3 系统异常时调用
        - 平仓前记录日志快照
        - 按持仓市价挂单，不等待成交确认
        """
        if not self._health_check():
            logger.error("QMT 未连接，无法执行一键平仓")
            return

        logger.warning("QMT 一键平仓未实现（阶段四）")

    def emergency_buy_limit(self, symbol: str, quantity: int, price: float) -> OrderResult:
        """限价买入（阶段四实现，应急场景使用）。

        与普通 order 的区别：
        - 跳过飞书人工确认
        - 按指定限价下单

        Args:
            symbol: 股票代码
            quantity: 数量
            price: 限价

        Returns:
            OrderResult
        """
        logger.warning("QMT 限价买入未实现（阶段四）")
        return OrderResult(False, symbol, "buy", 0, 0, 0, message="QMT 实盘未启用（阶段四）")

    def query_history_orders(self, days: int = 1) -> list[dict[str, Any]]:
        """查询历史订单（阶段四实现）。

        Args:
            days: 查询天数

        Returns:
            历史订单列表
        """
        logger.warning("QMT 历史订单查询未实现（阶段四）")
        return []
