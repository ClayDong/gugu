#!/usr/bin/env python3
"""陈学长规则 LLM 提取 + 批量回测 PoC。

流程：
1. 读取陈学长视频转写文本（/tmp/chenxuezhang_transcript.txt）
2. 用 LLM（DeepSeek API）基于其交易哲学设计候选规则
3. 每条规则生成可回测的 SingleRuleStrategy 子类
4. 批量回测（抽样 100 只快速筛选）
5. 输出报告

用法：
    uv run python scripts/extract_chenxuezhang_rules_llm.py            # 全流程
    uv run python scripts/extract_chenxuezhang_rules_llm.py --dry-run  # 只提取规则不跑回测
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest import BacktestEngine  # noqa: E402
from gugu.strategies.base import Strategy  # noqa: E402

# ── 配置 ──────────────────────────────────────────────────
CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet_cache"
STOCK_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
TRANSCRIPT_PATH = Path("/tmp/chenxuezhang_transcript.txt")
LLM_API_KEY = "sk-34f4eece4acc4f50be05e72fd8294aea"
LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"


# ── 1. 读取转写文本 ────────────────────────────────────────


def read_transcript(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── 2. LLM 提取候选规则 ────────────────────────────────────


def extract_rules(text: str) -> list[dict]:
    """基于陈学长交易哲学，由 LLM 设计可量化规则。"""
    print("\n=== LLM 基于陈学长哲学设计候选规则 ===")
    print(f"模型: {LLM_MODEL}", flush=True)

    # 提取陈学长核心哲学：取前几篇 + 关键内容节选
    paras = text.split("\n")
    head = "\n".join(paras[:5])
    keywords = [
        "成交量",
        "量价",
        "放量",
        "缩量",
        "均线",
        "背离",
        "突破",
        "回踩",
        "上影线",
        "涨停",
        "断板",
        "反包",
        "资金",
        "盘口",
        "成本",
    ]
    relevant = [p for p in paras if any(k in p for k in keywords)]
    samples = [p[:500] for p in relevant[:15]]
    context = "博主交易哲学:\n" + head + "\n\n博主关键内容节选:\n" + "\n---\n".join(samples)

    prompt = f"""基于下面博主的交易哲学，设计5-10条可量化的A股买入规则。

博主的核心理念：
1. 只看资金——"老大来了没有"是唯一标准
2. 盘口优先——大盘走势、板块、基本面都是次要的
3. 保护收益——以保护收益为核心
4. 不格局——短线为主
5. 找"苗头"——出现转折迹象时入场

要求：
- 每条规则给出 pandas 布尔条件（用 close, high, low, volume, open, ma5, ma10, ma20, vol_ma5, vol_ma20, ma20_slope）
- 多条条件之间是 AND 关系
- 置信度 0.3~0.7，持仓 3~10 天

输出纯JSON数组，不要markdown、不要代码块标记：
[{{"name":"规则名","description":"说明","conditions":["条件1","条件2"],"confidence":0.5,"hold_days":5}}]

博主原文节选：
{context[:4000]}"""

    print("  发送 prompt 给 LLM...", flush=True)
    rules = _call_llm(prompt)
    if rules:
        print(f"  提取到 {len(rules)} 条规则", flush=True)
    else:
        print("  ❌ LLM 未返回有效规则", flush=True)
    return rules


def _call_llm(prompt: str) -> list[dict]:
    """调用 LLM API，返回解析后的规则列表。"""
    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{LLM_BASE_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一位A股量化交易策略研究员。只输出有效的JSON数组，不要任何推理过程、不要markdown代码块、不要额外文字。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 4096,
                },
            )
            data = resp.json()
            msg = data["choices"][0]["message"]
            content: str = msg.get("content", "") or ""

            # DeepSeek 有时把输出放在 reasoning_content 中
            if not content.strip():
                content = msg.get("reasoning_content", "") or ""

            if not content.strip():
                return []

            # 去除 markdown 代码块包装
            content = content.strip()
            if content.startswith("```"):
                first_nl = content.find("\n")
                if first_nl >= 0:
                    content = content[first_nl:].strip()
                if content.endswith("```"):
                    content = content[:-3].strip()
            if content.startswith("json"):
                content = content[4:].strip()
            if content.startswith("json"):
                content = content[4:].strip()

            return json.loads(content)
    except Exception as e:
        print(f"    [ERROR] LLM 调用/解析失败: {e}", file=sys.stderr, flush=True)
        return []


# ── 3. 生成可回测的策略 ────────────────────────────────────


class LLMExtractedRuleStrategy(Strategy):
    """LLM 提取的单规则策略。出场统一 max_hold_days。"""

    name = "chenxuezhang_llm"

    def __init__(self, rule_config: dict, max_hold_days: int = 5, params: dict | None = None):
        self.rule_config = rule_config
        self._max_hold = max_hold_days
        super().__init__(params=params)

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        result = df.copy()
        close = result["close"]
        high = result["high"]
        low = result["low"]
        volume = result["volume"]
        open_p = result["open"]

        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()
        ma20_slope = ma20 - ma20.shift(5)

        signals = pd.Series(0, index=result.index)
        conditions = self.rule_config.get("conditions", [])

        if conditions:
            combined = pd.Series(True, index=result.index)
            for cond_str in conditions:
                try:
                    cond_result = eval(cond_str)
                    combined &= cond_result.fillna(False)
                except Exception:
                    pass
            signals = combined.astype(int)

        result["signal"] = signals
        confidence = float(self.rule_config.get("confidence", 0.5))
        result["confidence"] = pd.Series(confidence, index=result.index)

        # 统一出场：max_hold 日强制退出
        result = self._apply_max_hold_exit(result, max_hold_days=self._max_hold)
        return result


# ── 4. 批量回测 ────────────────────────────────────────────


def load_stocks(min_rows: int = 250) -> dict[str, pd.DataFrame]:
    stocks: dict[str, pd.DataFrame] = {}
    for f in glob.glob(str(CACHE_DIR / "*_500d.parquet")):
        code = Path(f).stem.split("_")[0]
        if not code.startswith(STOCK_PREFIXES):
            continue
        try:
            df = pd.read_parquet(f)
            if "date" not in df.columns or len(df) < min_rows:
                continue
            df["date"] = pd.to_datetime(df["date"])
            if code not in stocks or len(df) > len(stocks[code]):
                stocks[code] = df.sort_values("date").reset_index(drop=True)
        except Exception:
            continue
    return stocks


def backtest_rule_config(
    rule_config: dict,
    stocks: dict[str, pd.DataFrame],
    max_stocks: int = 0,
) -> pd.DataFrame:
    rows = []
    hold = int(rule_config.get("hold_days", 5))
    stock_list = list(stocks.items())
    if max_stocks > 0:
        stock_list = random.sample(stock_list, min(max_stocks, len(stock_list)))

    for sym, df in stock_list:
        try:
            strategy = LLMExtractedRuleStrategy(rule_config=rule_config, max_hold_days=hold)
            engine = BacktestEngine(
                initial_capital=100_000.0,
                commission_rate=0.00025,
                stamp_tax=0.0005,
                slippage=0.002,
            )
            r = engine.run(strategy, df, sym)
            m = r.metrics
            rows.append(
                {
                    "symbol": sym,
                    "return": m.get("total_return", np.nan),
                    "sharpe": m.get("sharpe", np.nan),
                    "win_rate": m.get("win_rate", np.nan),
                    "trades": m.get("total_trades", 0),
                    "max_dd": m.get("max_drawdown", np.nan),
                }
            )
        except Exception:
            continue
    return pd.DataFrame(rows)


def summarize(out: pd.DataFrame, rule_name: str) -> dict:
    if out.empty:
        return {"name": rule_name, "n_stocks": 0}
    traded = out[out["trades"] > 0]
    if traded.empty:
        return {
            "name": rule_name,
            "n_stocks": len(out),
            "n_traded": 0,
            "total_trades": 0,
            "avg_return": 0.0,
            "avg_sharpe": 0.0,
            "avg_win_rate": 0.0,
            "positive_stocks": 0,
            "positive_ratio": 0.0,
        }
    return {
        "name": rule_name,
        "n_stocks": len(out),
        "n_traded": len(traded),
        "total_trades": int(traded["trades"].sum()),
        "avg_return": traded["return"].mean(),
        "avg_sharpe": traded["sharpe"].mean(),
        "avg_win_rate": traded["win_rate"].mean(),
        "positive_stocks": int((traded["return"] > 0).sum()),
        "positive_ratio": (traded["return"] > 0).mean(),
    }


# ── 5. 主流程 ──────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只提取规则不跑回测")
    ap.add_argument("--sample", type=int, default=100, help="快速筛选时的抽样股票数")
    args = ap.parse_args()

    # Step 1: 读取转写文本
    print("读取转写文本...")
    text = read_transcript(TRANSCRIPT_PATH)
    print(f"  共 {len(text)} 字符")

    # Step 2: LLM 提取规则
    rules = extract_rules(text)
    if not rules:
        print("❌ LLM 未提取到任何规则，退出")
        with open("/tmp/chenxuezhang_llm_rules.json", "w", encoding="utf-8") as f:
            json.dump({"error": "no_rules"}, f, ensure_ascii=False, indent=2)
        return

    # 保存提取结果
    with open("/tmp/chenxuezhang_llm_rules.json", "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    print(f"\n规则已保存到 /tmp/chenxuezhang_llm_rules.json ({len(rules)} 条)")

    if args.dry_run:
        print("\n=== LLM 设计的规则 ===")
        for i, r in enumerate(rules):
            conds = r.get("conditions", [])
            print(f"\n{i+1}. {r.get('name', '?')}")
            print(f"   描述: {r.get('description', '')}")
            for c in conds:
                print(f"   条件: {c}")
            print(f"   置信度: {r.get('confidence', 0.5)} | 持仓: {r.get('hold_days', 5)}天")
        return

    # Step 3: 加载数据
    print("\n加载 500d 数据...")
    stocks = load_stocks()
    print(f"  可用股票: {len(stocks)} 只")

    # Step 4: 批量回测
    print(f"\n=== 批量回测（抽样 {args.sample} 只）===")
    results = []
    for i, rule in enumerate(rules):
        name = rule.get("name", f"rule_{i}")
        print(f"\n[{i+1}/{len(rules)}] {name} ...", flush=True)
        t0 = time.time()
        out = backtest_rule_config(rule, stocks, max_stocks=args.sample)
        s = summarize(out, name)
        s["index"] = i
        results.append(s)
        print(
            f"  完成 {time.time()-t0:.0f}s | "
            f"有交易 {s['n_traded']}/{s['n_stocks']} 只 | "
            f"总交易 {s['total_trades']} 笔 | "
            f"Sharpe {s['avg_sharpe']:.3f} | 收益 {s['avg_return']*100:.2f}%"
        )

    # Step 5: 输出报告
    print(f"\n{'='*80}")
    print("=== LLM 设计规则回测报告（抽样 {args.sample} 只）===")
    print(
        f"\n{'规则名称':14s} {'有交易':>6s} {'总交易':>7s} {'平均收益':>9s} {'平均夏普':>9s} {'平均胜率':>9s} {'正收益占比':>9s}"
    )
    print("-" * 80)

    results.sort(key=lambda x: x.get("avg_sharpe", -999), reverse=True)
    good = [s for s in results if s.get("avg_sharpe", -999) > 0.3]

    for s in results:
        print(
            f"{s['name']:14s} {s['n_traded']:>4d}/{s['n_stocks']:<3d} "
            f"{s['total_trades']:>7d} {s['avg_return']*100:>8.2f}% "
            f"{s['avg_sharpe']:>9.3f} {s['avg_win_rate']*100:>8.1f}% "
            f"{s['positive_ratio']*100:>8.1f}%"
        )

    print(f"\n{'='*80}")
    if good:
        print(f"✅ 找到 {len(good)} 条有潜在 edge 的规则（Sharpe > 0.3）：")
        for s in good:
            idx = s["index"]
            rule = rules[idx]
            print(f"\n  {s['name']}")
            print(
                f"    Sharpe: {s['avg_sharpe']:.3f} | 收益: {s['avg_return']*100:.2f}% | 胜率: {s['avg_win_rate']*100:.1f}%"
            )
            for c in rule.get("conditions", []):
                print(f"    条件: {c}")
    else:
        print("❌ 所有 LLM 设计的规则在日线量化框架下均无真实 edge")

    # 保存结果
    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_rules": len(rules),
        "good_rules": len(good),
        "results": [
            {
                "name": s["name"],
                "sharpe": s["avg_sharpe"],
                "return": s["avg_return"],
                "win_rate": s["avg_win_rate"],
                "trades": s["total_trades"],
            }
            for s in results
        ],
    }
    with open("/tmp/chenxuezhang_llm_report.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存到 /tmp/chenxuezhang_llm_report.json")


if __name__ == "__main__":
    main()
