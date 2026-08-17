"""断板反包战法·首板观察池（战法链路第 1 阶段自动化）

补齐 scan_rebound_market.py 的缺口：它只覆盖"断板之后"的形态（STAGE3/4 伏击圈），
不维护"首板 → 等断板"的前端观察。本脚本实现完整链路：

    今日首板(近20日无涨停+当日涨停) ──→ 待断板 ──→ 断板(放量阴/不涨停) ──→ 洗盘
        ──→ 缩量护盘不破首板开盘价 = 伏击圈 ✅（移交 scan_rebound_market 判定买点 A/C）
        ──→ 破位(收盘 < 首板开盘价×0.97) = 形态失败 ✕
        ──→ 断板后再次涨停 = 反包涨停（移交 scan 买点C 判定）
        ──→ 首板后 >25 个交易日无结果 = 超时移出

用法（收盘后 cron）:
    .venv/bin/python scripts/first_board_pool.py --scan --update   # 每日 15:05
    .venv/bin/python scripts/first_board_pool.py --check           # 调试: 打印池状态
    .venv/bin/python scripts/first_board_pool.py --scan --update --state-file /tmp/x.json

无变化时 --scan --update 输出为空（cron no_agent 静默）；有变化输出摘要文本。
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd

ssl._create_default_https_context = ssl._create_unverified_context  # 不带括号

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "http://finance.qq.com/",
}

# 与 scan_rebound_market.py 同源的主板代码区间（不含创业板/科创板/北交所）
CODE_RANGES = {
    "sh_main": list(range(600000, 604000)) + list(range(605000, 606000)),
    "sz_main": list(range(1, 2000)) + list(range(2001, 3000)),
}

LIMIT_UP_PCT = 9.7  # 主板涨停判定（行情快照 percent 口径）
LIMIT_UP_RATIO = 0.097  # 主板涨停判定（K线 pct_change 比率口径）
SUPPORT_TOL = 0.97  # 护盘底线容忍：收盘不破 首板开盘价×0.97
SHRINK_RATIO = 0.55  # 护盘期缩量达标：最近3日最低量 / 断板日量 <= 0.55
MAX_DAYS = 25  # 首板后最长观察天数
MV_MIN, MV_MAX = 30.0, 300.0  # 首板观察池市值区间（宽口径，扫描器 50-150 亿是买点口径）


# ---------------------------------------------------------------- 数据层
def all_codes():
    codes = [f"sh{n:06d}" for n in CODE_RANGES["sh_main"]]
    codes += [f"sz{n:06d}" for n in CODE_RANGES["sz_main"]]
    return codes


def fetch_quotes(codes, batch=300, delay=0.02):
    """批量行情（扩展字段: name/price/prev_close/change_pct/turnover/float_mv/total_mv）。

    分隔符兼容：优先 ~，长度不足则按逗号（腾讯接口 2026-08 双格式并存）。
    """
    out = {}
    for i in range(0, len(codes), batch):
        chunk = codes[i : i + batch]
        url = "http://qt.gtimg.cn/q=" + ",".join(chunk)
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
                for line in raw.strip().split(";"):
                    line = line.strip()
                    if not line.startswith("v_"):
                        continue
                    try:
                        body = line.split('="', 1)[1].rstrip('"')
                        f = body.split("~")
                        if len(f) < 46:
                            f = body.split(",")
                        if len(f) < 46:
                            continue
                        code6 = f[2]
                        code = ("sh" if code6.startswith("6") else "sz") + code6
                        out[code] = {
                            "name": f[1],
                            "price": float(f[3] or 0),
                            "prev_close": float(f[4] or 0),
                            "change_pct": float(f[32] or 0),
                            "turnover": float(f[38] or 0),
                            "float_mv": float(f[44] or 0),
                            "total_mv": float(f[45] or 0),
                        }
                    except (ValueError, IndexError):
                        continue
                break
            except Exception:
                if attempt == 2:
                    print(f"  ⚠️ batch {i}-{i + batch} 拉取失败", flush=True)
                time.sleep(1 + attempt * 2)
        time.sleep(delay)
        if (i // batch) % 15 == 0:
            print(f"  行情进度 {i}/{len(codes)} 命中 {len(out)}", flush=True)
    return out


def fetch_kline(code, days=30, retries=3):
    for attempt in range(retries):
        try:
            url = (
                f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                f"param={code},day,,,{days},qfq"
            )
            req = urllib.request.Request(url, headers=HEADERS)
            raw = urllib.request.urlopen(req, timeout=15).read()
            d = json.loads(raw.decode("utf-8", errors="ignore"))
            node = d.get("data", {}).get(code, {})
            k = node.get("qfqday") or node.get("day") or []
            if not k:
                return None
            rows = [
                [r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in k
                if len(r) >= 6
            ]
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1 + attempt)
    return None


# ---------------------------------------------------------------- 状态机（纯函数，可单测）
def is_limit_up(row):
    """主板涨停：涨幅>=9.7% 且收盘贴近最高价（收盘=涨停价）

    注意：row['pct'] 为 pct_change 比率口径（0.097），非百分数。
    """
    if row.get("pct", 0) < LIMIT_UP_RATIO - 0.001:
        return False
    return (row["high"] - row["close"]) / row["close"] < 0.01


def is_first_board(df):
    """最后一日为首板：当日涨停 + 近20日无涨停"""
    n = len(df)
    if n < 25:
        return False
    ddf = df.copy()
    close_vals = ddf["close"].values
    pcts = np.zeros(n)
    pcts[1:] = close_vals[1:] / close_vals[:-1] - 1
    ddf["pct"] = pcts
    last = ddf.iloc[-1]
    if not is_limit_up(last):
        return False
    prev = ddf.iloc[max(0, n - 21) : n - 1]
    return not prev.apply(is_limit_up, axis=1).any()


def advance_state(code, item, df):
    """根据最新K线推进池内一只股票的状态。

    返回 (status, changed, detail)：
      status: 待断板/洗盘中/伏击圈/反包涨停/破位失败/超时
      changed: 状态是否变化（用于输出/日志）
    """
    if df is None or len(df) < 3:
        return item.get("status", "待断板"), False, "数据不足"
    n = len(df)
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    first_date = item.get("first_board_date")
    # 定位首板日
    try:
        idx0 = list(dates).index(first_date)
    except ValueError:
        idx0 = None
    if idx0 is None:
        # 首板日不在K线内（数据回滚/重命名），按最后一日判定
        return item.get("status", "待断板"), False, "首板日缺失"
    ddf = df.iloc[idx0:].copy()
    ddf["pct"] = ddf["close"].pct_change()
    ddf.loc[ddf.index[0], "pct"] = item.get("first_pct", 0) / 100.0  # 首板日涨幅回填
    support = item.get("support", item.get("first_open", 0) * SUPPORT_TOL)

    rows = ddf.to_dict("records")
    first = rows[0]
    # 首板日之后逐日判定
    broken = None  # 断板日 index（相对 rows）
    failed = None  # 破位日
    rebound = None  # 断板后再涨停日
    for j in range(1, len(rows)):
        r = rows[j]
        if broken is None:
            if not is_limit_up(r):
                broken = j
        else:
            if is_limit_up(r):
                rebound = j
                break
            if r["close"] < support:
                failed = j
                break

    last = rows[-1]
    last_date = str(dates[-1])
    days_since_first = n - 1 - idx0

    # 超时：首板后超过 MAX_DAYS 交易日仍未进入终态
    if broken is None and days_since_first > MAX_DAYS:
        return "超时", True, f"首板后{days_since_first}日未断板"

    if broken is None:
        return "待断板", False, f"首板后{last_date}仍连板/未断板"

    if rebound is not None:
        rb = rows[rebound]
        return (
            "反包涨停",
            True,
            (
                f"断板({str(dates[idx0 + broken])[:10]})后再涨停 "
                f"{rb['close']:.2f}，移交 scan 买点C判定"
            ),
        )

    if failed is not None:
        fd = rows[failed]
        return (
            "破位失败",
            True,
            (f"{str(dates[idx0 + failed])[:10]}收盘{fd['close']:.2f}" f"< 支撑{support:.2f}"),
        )

    # 断板后未破位：判定缩量 → 伏击圈
    days_since_break = days_since_first - broken
    if days_since_break >= 2:
        vol_since_break = [r["volume"] for r in rows[broken:]]
        if vol_since_break:
            min_recent = (
                min(vol_since_break[-3:]) if len(vol_since_break) >= 3 else min(vol_since_break)
            )
            break_vol = rows[broken]["volume"]
            shrink = min_recent / break_vol if break_vol > 0 else 1.0
            if shrink <= SHRINK_RATIO:
                return (
                    "伏击圈",
                    item.get("status") != "伏击圈",
                    (
                        f"缩量{shrink:.0%} 未破位，进入伏击圈（首板开{item.get('first_open', 0):.2f} "
                        f"支撑{support:.2f} 现价{last['close']:.2f}）"
                    ),
                )

    return "洗盘中", item.get("status") not in ("洗盘中", "伏击圈"), f"断板后第{days_since_break}日"


# ---------------------------------------------------------------- 状态持久化
def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"updated": None, "active": {}}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def log_event(events_path, code, name, event, detail):
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "code": code,
        "name": name,
        "event": event,
        "detail": detail,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ---------------------------------------------------------------- 主流程
def scan_new_first_boards(state, events_path, verbose=True):
    """全市场扫描当日首板并入池。返回新增列表。"""
    print("== 首板池: 全市场行情拉取 ==", flush=True)
    quotes = fetch_quotes(all_codes())
    cands = []
    for code, q in quotes.items():
        if q["change_pct"] < LIMIT_UP_PCT - 0.3:
            continue
        if not (MV_MIN <= q["total_mv"] <= MV_MAX):
            continue
        if q["price"] <= 3:
            continue
        name = q["name"]
        if "ST" in name.upper() or "退" in name:
            continue
        cands.append((code, q))
    print(f"涨停候选(主板, 市值{MV_MIN:.0f}-{MV_MAX:.0f}亿, 非ST): {len(cands)} 只", flush=True)

    active_codes = set(state.get("active", {}))
    added = []
    for code, q in cands:
        if code in active_codes:
            continue
        df = fetch_kline(code, days=30)
        if df is None or not is_first_board(df):
            continue
        last = df.iloc[-1]
        item = {
            "code": code,
            "name": q["name"],
            "first_board_date": last["date"].strftime("%Y-%m-%d"),
            "first_open": round(float(last["open"]), 2),
            "first_close": round(float(last["close"]), 2),
            "first_pct": round(float(last["close"] / last["open"] - 1) * 100, 2)
            if last["open"]
            else 0.0,
            "support": round(float(last["open"]) * SUPPORT_TOL, 2),
            "total_mv": round(q["total_mv"], 1),
            "status": "待断板",
            "break_date": None,
            "entered": datetime.now().strftime("%Y-%m-%d"),
        }
        # 用 pct_change 校验涨停（qt 快照 prev_close 与 K线前收可能差一天）
        state.setdefault("active", {})[code] = item
        log_event(
            events_path,
            code,
            q["name"],
            "NEW_FIRST_BOARD",
            f"首板 {item['first_board_date']} 开{item['first_open']:.2f} "
            f"收{item['first_close']:.2f} 市值{item['total_mv']:.0f}亿",
        )
        added.append(item)
        print(
            f"  🆕 首板入池 {code} {q['name']} 开{item['first_open']:.2f} "
            f"收{item['first_close']:.2f} 市值{item['total_mv']:.0f}亿",
            flush=True,
        )
        time.sleep(0.15)  # K线单发限速
    return added


def update_pool(state, events_path, verbose=True):
    """推进池内所有股票状态。返回变更列表。"""
    changed = []
    remove = []
    active = state.get("active", {})
    for code, item in active.items():
        df = fetch_kline(code, days=40)
        status, is_changed, detail = advance_state(code, item, df)
        item["last_close"] = (
            round(float(df["close"].iloc[-1]), 2) if df is not None and len(df) else None
        )
        item["last_date"] = str(df["date"].iloc[-1])[:10] if df is not None and len(df) else None
        item["status"] = status
        if is_changed:
            event = {
                "待断板": "BROKEN",
                "洗盘中": "WASHING",
                "伏击圈": "VICINITY",
                "反包涨停": "REBOUND_UP",
                "破位失败": "FAILED",
                "超时": "TIMEOUT",
            }.get(status, "STATUS")
            log_event(events_path, code, item.get("name", ""), event, detail)
            changed.append((code, item.get("name", ""), status, detail))
            print(f"  📌 {code} {item.get('name', '')}: {status} — {detail}", flush=True)
        if status in ("破位失败", "超时", "反包涨停"):
            remove.append(code)
    for code in remove:
        active.pop(code, None)
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true", help="全市场扫描当日首板入池")
    parser.add_argument("--update", action="store_true", help="推进池内状态")
    parser.add_argument("--check", action="store_true", help="打印当前池状态")
    parser.add_argument("--state-file", default=str(ROOT / "data" / "first_board_pool.json"))
    parser.add_argument(
        "--events-file", default=str(ROOT / "data" / "first_board_pool_events.jsonl")
    )
    args = parser.parse_args()

    state_path = __import__("pathlib").Path(args.state_file)
    events_path = __import__("pathlib").Path(args.events_file)
    state = load_state(state_path)

    if args.check:
        active = state.get("active", {})
        print(f"首板观察池: 活跃 {len(active)} 只")
        for code, item in active.items():
            print(
                f"  {code} {item.get('name', '')} [{item.get('status')}] "
                f"首板{item.get('first_board_date')} 开{item.get('first_open')} "
                f"支撑{item.get('support')} 市值{item.get('total_mv')}亿"
            )
        return

    lines = []
    if args.scan:
        added = scan_new_first_boards(state, events_path)
        if added:
            names = "、".join(
                f"{a['name']}({a['code'].replace('sh', '').replace('sz', '')})" for a in added
            )
            lines.append(f"🆕 今日首板 {len(added)} 只: {names}")
    if args.update:
        changed = update_pool(state, events_path)
        for code, name, status, detail in changed:
            lines.append(f"📌 {name}: {status} — {detail}")
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)
    n_active = len(state.get("active", {}))
    lines.append(f"📊 活跃池 {n_active} 只")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
