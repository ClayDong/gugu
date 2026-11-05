"""换手+量比策略 · 实盘信号扫描脚本。

对指定股票池拉取腾讯历史K线 + 实时流通股本，运行 price_volume_signal 策略，
输出每只股票的最新信号状态、历史买入触发次数、以及近期(最后30日)的信号点。

两种模式：
- 默认(CLI)：打印完整明细，适合手动调试
- --push  ：输出"聚焦推送文本"，仅在有信号/接近信号时才打印（供 cron 投递飞书，
             无信号时输出为空 → Hermes cron 静默不打扰）

用法:
    python scripts/scan_price_volume.py [代码1,代码2,...]
    python scripts/scan_price_volume.py --push
不带参数时扫描 strategy_defaults 的 default_stock_pool。
"""

import argparse
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

# 从 gugu 项目根目录的 src 导入（Path(__file__).parent.parent = gugu/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
import pandas as pd
import yaml

from gugu.notifier.disclaimer import with_disclaimer

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from gugu.data.collectors.tencent_collector import TencentCollector
from gugu.strategies.price_volume import PriceVolumeSignalStrategy

TENCENT_REALTIME = "https://qt.gtimg.cn/q=PROMPT"
_CTX = ssl._create_unverified_context()

# 信号触发 / 接近阈值（百分比数值，与 today_turn=换手×100 同单位）
TURN_MIN_PCT = 5.0  # 换手 >5% 是启动/加速前提
VR_MIN = 2.0  # 量比 >=2 进入启动区
VR_CLOSE = 1.5  # 量比 1.5~2 视为"接近启动"
TURN_CLOSE_PCT = 4.0  # 换手 4~5% 且量比达标视为"接近启动"


def fetch_float_shares(symbol: str) -> tuple[float | None, str, float]:
    prefix = (
        "sh" if symbol.startswith(("6", "9")) else ("bj" if symbol.startswith(("4", "8")) else "sz")
    )
    url = TENCENT_REALTIME.replace("PROMPT", f"{prefix}{symbol}")
    req = urllib.request.Request(url, headers={"Referer": "http://finance.qq.com/"})
    data = urllib.request.urlopen(req, context=_CTX, timeout=15).read().decode("gbk")
    fields = data.split("=")[1].strip('"').split("~")
    price = float(fields[3]) if len(fields) > 3 else 0
    name = fields[1] if len(fields) > 1 else ""
    circ_mv_yi = float(fields[44]) if len(fields) > 44 else 0
    fs = circ_mv_yi * 1e8 / price if price > 0 and circ_mv_yi > 0 else None
    return fs, name, price


def _classify_today(today_turn: float, today_vr: float):
    """将今日换手/量比归类为: triggered / near / none（today_turn 为百分比数值）。"""
    if today_turn > TURN_MIN_PCT and today_vr >= VR_MIN and today_vr < 5.0:
        return "buy"
    if today_turn > TURN_MIN_PCT and today_vr >= 5.0:
        return "big"  # 大幅放量（需结合位置判断，脚本仅提示）
    if (today_turn > TURN_MIN_PCT and today_vr >= VR_CLOSE) or (
        today_turn > TURN_CLOSE_PCT and today_vr >= VR_MIN
    ):
        return "near"
    return None


def scan_pool(pool: list[str]):
    """扫描指定股票池，返回 (rows, triggered_last30, near_today)。"""
    tc = TencentCollector()
    rows = []
    triggered_last30 = []  # 近30日触发过买入的股票
    near_today = []  # 今日接近触发的股票
    buy_today = []  # 今日满仓触发买入
    for code in pool:
        try:
            fs, name, price = fetch_float_shares(code)
            if not fs:
                rows.append(
                    {
                        "code": code,
                        "name": "",
                        "price": 0,
                        "state": "error",
                        "note": "流通股本获取失败",
                    }
                )
                continue
            df = tc.fetch_stock_history(code, days=250)
            if df is None or df.empty:
                rows.append(
                    {
                        "code": code,
                        "name": name or code,
                        "price": price,
                        "state": "error",
                        "note": "无数据",
                    }
                )
                continue
            s = PriceVolumeSignalStrategy(params={"float_shares": fs})
            out = s.generate_signals(df.copy(), is_backtest=True)
            med_turn = out["turnover"].iloc[-60:].median() * 100
            last30_buy = (out["signal"].iloc[-30:] == 1).sum()
            last_sig = int(out["signal"].iloc[-1])
            today_turn = out["turnover"].iloc[-1] * 100
            today_vr = float(out["volume_ratio"].iloc[-1])
            state = _classify_today(today_turn, today_vr)

            r = {
                "code": code,
                "name": name or code,
                "price": price,
                "state": state,
                "med_turn": med_turn,
                "last30_buy": last30_buy,
                "last_sig": last_sig,
                "today_turn": today_turn,
                "today_vr": today_vr,
                "note": "",
            }
            if state == "buy":
                buy_today.append(r)
            elif state in ("near", "big"):
                near_today.append(r)
            if last30_buy > 0:
                triggered_last30.append(r)
            rows.append(r)
        except Exception as e:
            rows.append({"code": code, "name": code, "price": 0, "state": "error", "note": str(e)})
    return rows, triggered_last30, near_today, buy_today


def build_push_text(rows, triggered_last30, near_today, buy_today) -> str:
    """生成聚焦推送文本。无任何值得关注的信号时返回空串（供 cron 静默，避免噪声）。"""
    if not (buy_today or near_today or triggered_last30):
        return ""
    today = pd.Timestamp.now().strftime("%m-%d")
    lines = [f"📡 换手+量比选股观察 · {today}"]

    if buy_today:
        lines.append("\n🔴 **今日触发买入信号**")
        for r in buy_today:
            lines.append(
                f"· {r['name']}({r['code']}) 现价{r['price']:.2f} "
                f"换手{r['today_turn']:.1f}% 量比{r['today_vr']:.1f} ✅"
            )
    else:
        lines.append("\n今日无满仓买入触发（稀有信号，属正常）")

    if near_today:
        lines.append("\n🟡 **接近触发·待观察**")
        for r in near_today:
            tag = {None: "", "near": "接近", "big": "大幅放量"}[r.get("state")]
            lines.append(
                f"· {r['name']}({r['code']}) 现价{r['price']:.2f} "
                f"换手{r['today_turn']:.1f}% 量比{r['today_vr']:.1f} {tag}"
            )
    else:
        lines.append("\n暂无接近触发的观察标的")

    if triggered_last30:
        lines.append("\n📊 **近30日触发过买入**（趋势参考）")
        for r in triggered_last30:
            lines.append(f"· {r['name']}({r['code']}) {r['last30_buy']}次")

    return "\n".join(lines)


def _load_env():
    """加载 gugu 项目根 .env（若可用）。幂等，供 --feishu-push 使用。"""
    if load_dotenv is None:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=str(env_path), override=False)


def _fetch_tenant_token(app_id: str, app_secret: str) -> str | None:
    """换飞书 app 的 tenant_access_token。失败返回 None。"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, context=_CTX, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("code") == 0:
            return data.get("tenant_access_token")
        return None
    except Exception:
        return None


def _push_feishu_text(text: str, chat_id: str | None = None) -> bool:
    """以 gugu bot(app) 身份把纯文本消息发到飞书 chat_id。成功返回 True，失败返回 False。

    目标群优先级（2026-10-20 修正：业务观察消息发"开悟之路"业务群，不再发 Hermes 对话框）：
    --chat 参数 > .env FEISHU_BUSINESS_CHAT_ID（业务群）> .env FEISHU_CHAT_ID（系统兜底）。

    依赖 .env 中的 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BUSINESS_CHAT_ID。
    脚本内发起 HTTP，全程不依赖 Hermes cron 投递，规避 no_agent 投递的飞书 99992402 问题。
    """
    try:
        _load_env()
        # 所有对外推送统一带免责（实验性信号不得包装成投资建议）
        text = with_disclaimer(text)
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        chat = (
            chat_id or os.environ.get("FEISHU_BUSINESS_CHAT_ID") or os.environ.get("FEISHU_CHAT_ID")
        )
        if not (app_id and app_secret and chat):
            return False
        token = _fetch_tenant_token(app_id, app_secret)
        if not token:
            return False
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = json.dumps(
            {
                "receive_id": chat,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
        ).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, context=_CTX, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("code") == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument(
        "pool", nargs="?", default=None, help="逗号分隔的股票代码，缺省用 default_stock_pool"
    )
    ap.add_argument(
        "--push",
        action="store_true",
        help="扫描并飞书推送（推送到 --chat 或 .env FEISHU_BUSINESS_CHAT_ID 即开悟之路群；成功则 stdout 静默，失败则 stdout 输出文本兜底）",
    )
    ap.add_argument("--preview", action="store_true", help="只打印推送文本，不发送飞书")
    ap.add_argument(
        "--chat", default=None, help="飞书 chat_id 覆盖，缺省用 .env FEISHU_BUSINESS_CHAT_ID"
    )
    ap.add_argument("--pool-only", action="store_true", help="只打印股票池列表")
    args = ap.parse_args()

    defaults = yaml.safe_load(
        open(Path(__file__).resolve().parent.parent / "config/strategy_defaults.yaml")
    )
    default_pool = defaults["price_volume_signal"]["default_stock_pool"].split(",")

    pool = []
    if args.pool:
        pool = args.pool.split(",")
    pool = [c.strip().zfill(6) for c in (pool or default_pool)]

    if args.pool_only:
        print(",".join(pool))
        return

    rows, triggered_last30, near_today, buy_today = scan_pool(pool)

    if args.push:
        # --preview: 只打印推送文本（供调试/手动查看）
        if args.preview:
            text = build_push_text(rows, triggered_last30, near_today, buy_today)
            print(text if text else "(无信号，当天静默)")
            return
        # 扫描 + 飞书推送：成功则 stdout 静默（cron 不投递、无 delivery error），失败则 stdout 输出文本兜底
        text = build_push_text(rows, triggered_last30, near_today, buy_today)
        if not text:
            return  # 无信号，静默
        ok = _push_feishu_text(text, args.chat)
        if not ok:
            print(text)  # 推送失败 → stdout 交给 cron 尝试投递（兜底）
        return

    # ── 默认 CLI 详细输出 ──
    print(f"=== 换手+量比策略 实盘扫描（{len(pool)} 只）===\n")
    print(f"{'代码':<8}{'名称':<10}{'现价':>8}{'换手中位%':>9}{'近30日买入':>10}{'今日信号':>8}")
    print("-" * 60)
    for r in rows:
        tag = {"buy": "✅买入", "near": "🟡接近", "big": "🔺放量", None: "·", "error": "✗"}[
            r["state"]
        ]
        print(
            f"{r['code']:<8}{r['name']:<10}{r['price']:>8.2f}{r['med_turn']:>9.2f}{r['last30_buy']:>10}{tag:>8}"
        )
        if r["state"] != "error":
            extra = (
                f"今日换手{r['today_turn']:.2f}% 量比{r['today_vr']:.2f} "
                f"{'→ 触发买入条件' if r['state']=='buy' else ('→ 接近触发' if r['state']=='near' else ('→ 大幅放量' if r['state']=='big' else ''))}"
            )
            print(f"        {extra}")
        else:
            print(f"        {r['note']}")


if __name__ == "__main__":
    main()
