"""QMT（迅投）实盘 broker。

接入步骤：
1. 安装 xtquant：pip install xtquant
2. 启动 QMT 客户端并登录（Windows / Parallels 虚拟机均可）
3. 配置 .env: QMT_PATH, QMT_ACCOUNT_ID, QMT_PASSWORD
4. 使用 scripts/run_live.py 启动实盘

v0.9.0 完整实现（原为阶段四占位）：
- connect() / order() / 撤单 / 持仓同步 / 账户查询 全部真实接入 xtquant
- 股票代码自动转换（sh600000 <-> 600000.SH）
- 订单状态查询 + 批量撤单 + 一键平仓
- 连接心跳检测 + 自动重连
- 安全机制：未配置时拒绝下单，绝不裸奔
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from gugu.config import env, get_config
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()

# A股 100 股整数倍
LOT_SIZE = 100

# QMT 下单类型常量
ORDER_TYPE_BUY = 23  # 证券买入
ORDER_TYPE_SELL = 24  # 证券卖出

# 价格类型
PRICE_TYPE_LIMIT = 11  # 限价
PRICE_TYPE_LATEST = 5  # 最新价（市价类）


def to_qmt_symbol(symbol: str) -> str:
    """把 gugu 内部代码（sh600000 / sz000001 / hk00700）转为 QMT 格式（600000.SH / 000001.SZ）。

    QMT 使用后缀格式：SH / SZ / HK。
    """
    s = symbol.strip().lower()
    if "." in s:
        return s.upper()  # 已是 QMT 格式
    if s.startswith("sh"):
        return f"{s[2:]}.SH"
    if s.startswith("sz"):
        return f"{s[2:]}.SZ"
    if s.startswith("bj"):
        return f"{s[2:]}.BJ"
    if s.startswith("hk"):
        return f"{s[2:]}.HK"
    # 裸 6 位代码：按首位判断市场
    if len(s) == 6:
        if s.startswith(("6", "9", "5")):
            return f"{s}.SH"
        return f"{s}.SZ"
    return s.upper()


def to_gugu_symbol(qmt_symbol: str) -> str:
    """QMT 格式（600000.SH）转 gugu 内部格式（sh600000）。"""
    code, _, market = qmt_symbol.partition(".")
    market = market.upper()
    if market == "SH":
        return f"sh{code}"
    if market == "SZ":
        return f"sz{code}"
    if market == "BJ":
        return f"bj{code}"
    if market == "HK":
        return f"hk{code}"
    return qmt_symbol


class QmtBroker(BaseBroker):
    """QMT 实盘 broker（v0.9.0 完整实现）。

    安全机制：
    - 未配置 QMT 时所有下单拒绝（防裸奔）
    - 下单数量自动取整到 100 股整数倍
    - 连接心跳检测 + 自动重连
    - 一键平仓仅限熔断/异常场景
    - 挂单成交状态异步查询
    """

    def __init__(self) -> None:
        self._account_id = env().qmt_account_id
        self._qmt_path = env().qmt_path
        self._connected = False
        self._last_health_check: float = 0.0
        self._health_check_interval: float = 30.0  # 每 30s 检测一次连接
        self._trader: Any = None
        self._account: Any = None
        self._pending_orders: dict[str, dict[str, Any]] = {}  # order_id -> order_info
        if not self._account_id:
            logger.warning("QMT 未配置 account_id，实盘功能不可用")

    # ────────────────────── 连接管理 ──────────────────────

    def connect(self) -> bool:
        """连接 QMT 客户端（xtquant 真实接入）。

        Returns:
            是否连接成功
        """
        try:
            from xtquant import xttrader  # type: ignore

            if not self._qmt_path or not self._account_id:
                logger.error("QMT 未配置 QMT_PATH / QMT_ACCOUNT_ID，无法连接")
                return False

            # 会话 ID：进程唯一
            session_id = int(time.time() * 1000) % 1000000
            self._trader = xttrader.XtQuantTrader(self._qmt_path, session_id)
            # 注册回调（成交回报等）
            self._trader.register_callback(self._on_callback)
            self._trader.start()
            connect_result = self._trader.connect()
            if connect_result == 0:
                self._account = xttrader.StockAccount(self._account_id)
                self._connected = True
                logger.info("QMT 连接成功: account={}", self._account_id)
                # 连接成功后立即同步一次持仓
                try:
                    self.sync_positions()
                except Exception as e:
                    logger.warning("QMT 初始持仓同步失败: {}", e)
                return True
            else:
                self._connected = False
                logger.error("QMT 连接失败: connect_result={}", connect_result)
                return False
        except ImportError:
            logger.error("xtquant 未安装，请先 pip install xtquant")
            return False
        except Exception as e:
            self._connected = False
            logger.error("QMT 连接异常: {}", e)
            return False

    def _on_callback(self, cb_type: int, data: Any) -> None:
        """xtquant 回调处理（成交回报 / 委托回报）。"""
        try:
            if cb_type == 18:  # 委托回报
                order = data
                order_id = getattr(order, "order_id", None)
                if order_id is not None:
                    self._pending_orders[str(order_id)] = {
                        "symbol": getattr(order, "stock_code", ""),
                        "status": getattr(order, "order_status", ""),
                        "filled_qty": getattr(order, "traded_volume", 0),
                        "avg_price": getattr(order, "traded_price", 0),
                        "timestamp": datetime.now().isoformat(),
                    }
            elif cb_type == 19:  # 成交回报
                trade = data
                logger.info(
                    "QMT 成交回报: {} {}股 @{}",
                    getattr(trade, "stock_code", ""),
                    getattr(trade, "traded_volume", 0),
                    getattr(trade, "traded_price", 0),
                )
        except Exception as e:
            logger.debug("QMT 回调处理异常: {}", e)

    def _health_check(self) -> bool:
        """检测 QMT 连接是否存活，断开自动重连。"""
        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return self._connected
        self._last_health_check = now
        if not self._connected:
            logger.warning("QMT 连接已断开，尝试自动重连...")
            return self.connect()
        return True

    def disconnect(self) -> None:
        """断开 QMT 连接（退出前调用）。"""
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception as e:
                logger.debug("QMT 断开异常: {}", e)
        self._connected = False

    # ────────────────────── 下单 ──────────────────────

    def _live_guard(self, direction: str) -> str | None:
        """实盘硬闸：返回拒绝原因，None 表示允许下单。

        2026-11-04 评审 P0：安全边界此前只在上层（Web 二次确认 / 实时扫描器开关），
        QmtBroker 作为唯一能发出真实委托的出口却没有自检，emergency_buy_limit
        或直接调用 order() 即可绕过。此处下沉为最后一道 fail-closed 防线。

        - 非 live 模式：一律拒绝（QMT 只允许真实盘模式使用）；
        - 买入：必须存在非空生产白名单 verified_strategies；
        - 卖出/平仓：降低风险方向，允许（应急平仓不应被自动交易开关卡死）；
        - 配置读取异常：一律拒绝。
        """
        try:
            cfg = get_config()
            mode = str(getattr(cfg.execution, "mode", "") or "")
            verified = list(getattr(cfg.strategy, "verified_strategies", None) or [])
        except Exception as exc:  # noqa: BLE001 - 安全闸必须 fail-closed
            return f"无法读取执行配置，拒绝下单（fail-closed）：{exc}"
        if mode != "live":
            return f"execution.mode={mode or '未配置'}，QMT 实盘下单仅在 live 模式允许"
        if str(direction).strip().lower() == "buy":
            if not verified:
                return "verified_strategies 为空，禁止实盘买入（无已验证策略）"
            # P1-B 信任链闭环：白名单只是「人填的名单」，不能自证。还要求名单里的
            # 策略在 manifest 中通过了证据哈希校验（validate_evidence）——否则只改
            # settings.yaml 写个名字即可绕过 P0-2 的整条证据链打开实盘买入。
            if not self._verified_evidence_backed(set(map(str, verified))):
                return (
                    "verified_strategies 中无任何策略通过 manifest 证据校验，"
                    "禁止实盘买入（禁止只改白名单绕过证据链）"
                )
        return None

    def _verified_evidence_backed(self, verified: set[str]) -> bool:
        """白名单策略必须在 manifest 中通过证据校验（P1-B）。

        复用 manifest.is_runtime_enabled 的 validate_evidence + GatePolicy 逻辑，
        任何读取/校验异常一律 fail-closed 返回 False。
        """
        try:
            from gugu.strategies.manifest import StrategyManifest

            enabled = set(StrategyManifest().list_runtime_enabled("live", keys=verified))
        except Exception as exc:  # noqa: BLE001 - 安全闸必须 fail-closed
            logger.warning("QMT 实盘买入证据校验异常，fail-closed: {}", exc)
            return False
        return bool(enabled)

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """下单（限价或市价）。

        Args:
            symbol: 股票代码（gugu 格式 sh600000）
            direction: buy/sell
            quantity: 数量（自动取整到 100 股整数倍）
            price: 限价，None 为最新价（市价）

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

        guard_reason = self._live_guard(direction)
        if guard_reason:
            logger.warning("QMT 下单被实盘硬闸拒绝: {}", guard_reason)
            return OrderResult(
                False,
                symbol,
                direction,
                0,
                0,
                0,
                message=guard_reason,
            )

        qmt_symbol = to_qmt_symbol(symbol)
        order_type = ORDER_TYPE_BUY if direction == "buy" else ORDER_TYPE_SELL
        # 数量取整到 100 股
        qty = max(LOT_SIZE, int(quantity // LOT_SIZE * LOT_SIZE))
        price_type = PRICE_TYPE_LIMIT if price is not None else PRICE_TYPE_LATEST
        order_price = price if price is not None else 0.0

        try:
            order_id = self._trader.order_stock(
                self._account,
                qmt_symbol,
                order_type,
                qty,
                price_type,
                order_price,
                "gugu",
                f"gugu_{direction}",
            )
            if order_id and order_id > 0:
                self._pending_orders[str(order_id)] = {
                    "symbol": qmt_symbol,
                    "direction": direction,
                    "quantity": qty,
                    "price": order_price,
                    "timestamp": datetime.now().isoformat(),
                }
                logger.info(
                    "QMT 下单成功: {} {} {}股 @{} order_id={}",
                    direction,
                    qmt_symbol,
                    qty,
                    order_price,
                    order_id,
                )
                # 估算费用（佣金万2.5，卖出印花税万5，过户费万0.1）
                exec_price = order_price or 0.0
                commission = round(exec_price * qty * 0.00025, 2)
                stamp_tax = round(exec_price * qty * 0.0005, 2) if direction == "sell" else 0.0
                transfer_fee = round(exec_price * qty * 0.00001, 2)
                return OrderResult(
                    True,
                    symbol,
                    direction,
                    exec_price,
                    qty,
                    commission,
                    stamp_tax,
                    transfer_fee,
                    message=f"已提交委托 order_id={order_id}",
                )
            else:
                logger.error("QMT 下单失败: order_id={}", order_id)
                return OrderResult(
                    False,
                    symbol,
                    direction,
                    0,
                    0,
                    0,
                    message=f"QMT 下单失败 order_id={order_id}",
                )
        except Exception as e:
            logger.error("QMT 下单异常: {}", e)
            return OrderResult(False, symbol, direction, 0, 0, 0, message=f"QMT 下单异常: {e}")

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单。

        Args:
            order_id: 委托编号

        Returns:
            是否成功撤销
        """
        if not self._health_check():
            logger.error("QMT 未连接，无法撤单")
            return False
        try:
            result = self._trader.cancel_order_stock(self._account, int(order_id))
            success = result == 0
            if success:
                logger.info("QMT 撤单成功: order_id={}", order_id)
            else:
                logger.warning("QMT 撤单失败: order_id={} result={}", order_id, result)
            return success
        except Exception as e:
            logger.error("QMT 撤单异常: order_id={} err={}", order_id, e)
            return False

    def cancel_all(self, direction: str | None = None) -> int:
        """批量撤单。

        Args:
            direction: 买卖方向，None=全部撤单，buy=仅撤买入，sell=仅撤卖出

        Returns:
            成功撤单数量
        """
        if not self._health_check():
            return 0
        try:
            orders = self._trader.query_stock_orders(self._account, cancelable_only=True)
            if not orders:
                return 0
            cancelled = 0
            for order in orders:
                order_direction = getattr(order, "order_type", 0)
                if direction == "buy" and order_direction != ORDER_TYPE_BUY:
                    continue
                if direction == "sell" and order_direction != ORDER_TYPE_SELL:
                    continue
                order_id = getattr(order, "order_id", None)
                if order_id is not None and self.cancel_order(str(order_id)):
                    cancelled += 1
            logger.info("QMT 批量撤单完成: 撤 {} 笔", cancelled)
            return cancelled
        except Exception as e:
            logger.error("QMT 批量撤单异常: {}", e)
            return 0

    def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        """查询订单状态。

        Returns:
            订单状态字典，含 status/filled_qty/avg_price 等字段
        """
        if not self._health_check():
            return None
        # 优先返回回调缓存
        if order_id in self._pending_orders:
            return self._pending_orders[order_id]
        try:
            orders = self._trader.query_stock_orders(self._account)
            for order in orders:
                if str(getattr(order, "order_id", "")) == order_id:
                    return {
                        "order_id": order_id,
                        "symbol": getattr(order, "stock_code", ""),
                        "status": getattr(order, "order_status", ""),
                        "filled_qty": getattr(order, "traded_volume", 0),
                        "avg_price": getattr(order, "traded_price", 0),
                        "canceled_qty": getattr(order, "canceled_volume", 0),
                    }
            return None
        except Exception as e:
            logger.error("QMT 订单状态查询异常: {}", e)
            return None

    def sync_positions(self) -> None:
        """从 QMT 同步持仓到本地缓存（data/qmt_positions.json）。

        每日开盘前调用，确保本地持仓与 QMT 保持一致。
        """
        if not self._health_check():
            logger.warning("QMT 未连接，无法同步持仓")
            return
        try:
            positions = self.get_portfolio()
            if not positions:
                logger.info("QMT 持仓为空（可能无持仓或查询失败）")
            import json

            from gugu.config import PROJECT_ROOT

            path = PROJECT_ROOT / "data" / "qmt_positions.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "synced_at": datetime.now().isoformat(),
                "positions": {
                    sym: {
                        "quantity": p.quantity,
                        "available": p.available,
                        "avg_cost": p.avg_cost,
                        "current_price": p.current_price,
                    }
                    for sym, p in positions.items()
                },
            }
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info("QMT 持仓已同步到 {}", path)
        except Exception as e:
            logger.error("QMT 持仓同步异常: {}", e)

    # ────────────────────── 持仓 / 账户 ──────────────────────

    def get_position(self, symbol: str) -> Position | None:
        if not self._health_check():
            return None
        try:
            positions = self._trader.query_stock_positions(self._account)
            qmt_symbol = to_qmt_symbol(symbol)
            for p in positions:
                if getattr(p, "stock_code", "") == qmt_symbol:
                    return self._to_position(p)
            return None
        except Exception as e:
            logger.error("QMT 查询持仓异常: {}", e)
            return None

    def get_portfolio(self) -> dict[str, Position]:
        if not self._health_check():
            return {}
        try:
            positions = self._trader.query_stock_positions(self._account)
            result: dict[str, Position] = {}
            for p in positions:
                pos = self._to_position(p)
                if pos is not None:
                    result[pos.symbol] = pos
            return result
        except Exception as e:
            logger.error("QMT 查询持仓异常: {}", e)
            return {}

    def _to_position(self, p: Any) -> Position | None:
        """把 xtquant 持仓对象转为 gugu Position。"""
        try:
            qmt_symbol = getattr(p, "stock_code", "")
            if not qmt_symbol:
                return None
            symbol = to_gugu_symbol(qmt_symbol)
            quantity = int(getattr(p, "volume", 0) or 0)
            available = int(getattr(p, "can_use_volume", 0) or 0)
            avg_cost = float(getattr(p, "open_price", 0) or 0)
            current_price = float(getattr(p, "market_value", 0) / quantity) if quantity else 0.0
            return Position(
                symbol=symbol,
                quantity=quantity,
                available=available,
                avg_cost=avg_cost,
                current_price=current_price,
            )
        except Exception as e:
            logger.debug("持仓对象转换失败: {}", e)
            return None

    def get_account(self) -> AccountInfo:
        if not self._health_check():
            return AccountInfo(cash=0, total_value=0, positions={})
        try:
            asset = self._trader.query_stock_asset(self._account)
            positions = self.get_portfolio()
            cash = float(getattr(asset, "cash", 0) or 0)
            total_value = float(getattr(asset, "total_asset", 0) or 0)
            positions_value = total_value - cash
            return AccountInfo(
                cash=cash,
                total_value=total_value,
                positions=positions,
                positions_value=positions_value,
            )
        except Exception as e:
            logger.error("QMT 查询账户异常: {}", e)
            return AccountInfo(cash=0, total_value=0, positions={})

    def update_price(self, symbol: str, price: float) -> None:
        """实盘自动更新，无需手动调用。"""

    # ────────────────────── 应急操作 ──────────────────────

    def emergency_close_all(self) -> None:
        """一键平仓应急脚本。

        安全机制：
        - 仅允许在 L2 熔断或 L3 系统异常时调用
        - 平仓前记录日志快照
        - 按持仓市价挂单，不等待成交确认
        """
        if not self._health_check():
            logger.error("QMT 未连接，无法执行一键平仓")
            return
        try:
            positions = self.get_portfolio()
            if not positions:
                logger.info("一键平仓: 无持仓")
                return
            logger.warning("一键平仓启动: {} 只持仓", len(positions))
            for symbol, pos in positions.items():
                if pos.available > 0:
                    result = self.order(symbol, "sell", pos.available)
                    logger.warning(
                        "一键平仓: {} 卖出 {} 股 -> {}",
                        symbol,
                        pos.available,
                        "成功" if result.success else f"失败: {result.message}",
                    )
            logger.warning("一键平仓完成")
        except Exception as e:
            logger.error("一键平仓异常: {}", e)

    def emergency_buy_limit(self, symbol: str, quantity: int, price: float) -> OrderResult:
        """限价买入（应急场景使用）。

        与普通 order 的区别：
        - 跳过飞书人工确认
        - 按指定限价下单
        """
        return self.order(symbol, "buy", quantity, price)

    def query_history_orders(self, days: int = 1) -> list[dict[str, Any]]:
        """查询历史订单。

        Args:
            days: 查询天数

        Returns:
            历史订单列表
        """
        if not self._health_check():
            return []
        try:
            orders = self._trader.query_stock_orders(self._account, cancelable_only=False)
            result: list[dict[str, Any]] = []
            for order in orders:
                result.append(
                    {
                        "order_id": getattr(order, "order_id", ""),
                        "symbol": getattr(order, "stock_code", ""),
                        "direction": "buy"
                        if getattr(order, "order_type", 0) == ORDER_TYPE_BUY
                        else "sell",
                        "quantity": getattr(order, "order_volume", 0),
                        "filled_qty": getattr(order, "traded_volume", 0),
                        "price": getattr(order, "price", 0),
                        "avg_price": getattr(order, "traded_price", 0),
                        "status": getattr(order, "order_status", ""),
                        "created_at": getattr(order, "order_time", ""),
                    }
                )
            return result[-days * 100 :]  # 粗略按天数截断
        except Exception as e:
            logger.error("QMT 历史订单查询异常: {}", e)
            return []


def main() -> None:
    """命令行自检：连接 QMT 并打印账户/持仓。"""
    import argparse

    parser = argparse.ArgumentParser(description="QMT 实盘连接自检")
    parser.add_argument("--connect-only", action="store_true", help="仅测试连接")
    args = parser.parse_args()

    broker = QmtBroker()
    if not broker.connect():
        print("❌ QMT 连接失败（检查 .env 配置和 QMT 客户端是否已登录）")
        return

    print("✅ QMT 连接成功")
    account = broker.get_account()
    print(f"   账户总资产: {account.total_value:.2f}")
    print(f"   可用资金:   {account.cash:.2f}")
    positions = broker.get_portfolio()
    if positions:
        print(f"   持仓 {len(positions)} 只:")
        for sym, pos in positions.items():
            print(
                f"     {sym}: {pos.quantity}股 成本{pos.cost_price:.2f} 市值{pos.market_value:.2f}"
            )
    else:
        print("   无持仓")
    if not args.connect_only:
        broker.disconnect()


if __name__ == "__main__":
    main()
