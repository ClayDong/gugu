"""ml_gradient_boost 模型训练脚本（2026-08-01，防前视偏差版）。

背景：
- ml_gradient_boost 曾因模型文件缺失成为"死配置"（enabled:true 但 0 信号），已降级 disabled。
- 本脚本按 Walk-Forward 铁律训练：训练集严格截止 2025-01-31（与事件研究切分一致），
  标签 = 未来 5 日收益。验证集（2025-02 之后）绝不参与训练——防止前视偏差。

用法：
    .venv/bin/python scripts/train_ml_gradient_boost.py --days 400 --pool 15股
    （只拉训练期前 400 日 K 线，标签自然落在训练期内）

输出：
    data/ml_models/gradient_boost_v1.pkl + gradient_boost_scaler_v1.pkl
    data/ml_gradient_boost_train_report.json
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
_CTX = ssl._create_unverified_context()

# 训练截止：2025-01-31（与事件研究 train 切分一致）
TRAIN_CUTOFF = pd.Timestamp("2025-01-31")

POOL = [
    "sz002472",
    "sz300033",
    "sz002230",
    "sh603501",
    "sz300308",
    "sz300502",
    "sh600519",
    "sz300750",
    "sz002594",
    "sz300059",
    "sz002475",
    "sh600050",
    "sz000977",
    "sh688041",
    "sz002371",
]


def _http_get(url: str, referer: str = "http://finance.qq.com/") -> str:
    req = urllib.request.Request(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_kline(symbol: str, days: int) -> pd.DataFrame:
    url = f"{TENCENT_KLINE}?param={symbol},day,,,{days},qfq"
    data = json.loads(_http_get(url))
    node = data.get("data", {}).get(symbol, {})
    kline = node.get("qfqday") or node.get("day") or []
    if not kline:
        return pd.DataFrame()
    rows = []
    for row in kline:
        if len(row) < 6:
            continue
        rows.append(
            [row[0], float(row[1]), float(row[3]), float(row[4]), float(row[2]), float(row[5])]
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.dropna(subset=["open", "high", "low", "close"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="训练期前拉取K线天数")
    ap.add_argument("--pool", nargs="*", default=POOL)
    ap.add_argument("--horizon", type=int, default=5, help="预测未来N日收益")
    args = ap.parse_args()

    from gugu.strategies.ml_gradient_boost.gradient_boost import (
        MLGradientBoostStrategy,
        train_and_save,
    )

    # 复用策略自身的特征提取（保证训练/预测特征完全一致，避免列数不匹配）
    strategy = MLGradientBoostStrategy()
    extractor = strategy._extract_features

    all_X, all_y, all_dates = [], [], []
    for symbol in args.pool:
        df = fetch_kline(symbol, args.days)
        if df.empty or len(df) < 250:
            print(f"  ⚠️ {symbol} 数据不足")
            continue
        df["date"] = pd.to_datetime(df["date"])
        feats = extractor(df)  # np.ndarray (n, n_features)
        close = df["close"]

        # 标签 = 未来 horizon 日收益（shift 前移，保证 y[t] 对应 x[t]）
        y = close.shift(-args.horizon) / close - 1

        # 训练集严格截止 TRAIN_CUTOFF，且去掉 NaN 行
        mask = df["date"] <= TRAIN_CUTOFF
        mask &= ~np.isnan(feats).any(axis=1) & y.notna()
        X = feats[mask]
        yv = y[mask]
        dates = df["date"][mask]
        print(f"  {symbol}: 训练样本 {len(X)}（截至 {TRAIN_CUTOFF.date()}）")
        all_X.append(X)
        all_y.append(yv)
        all_dates.append(dates)
        time.sleep(0.3)

    if not all_X:
        print("❌ 无训练数据")
        return

    X_all = np.vstack(all_X)
    y_all = pd.concat(all_y)
    n_features = X_all.shape[1]
    print(f"\n总训练样本: {len(X_all)}  特征数: {n_features}")
    print(
        f"标签分布: mean={y_all.mean():+.4f} std={y_all.std():.4f} "
        f"正样本占比={(y_all > 0).mean()*100:.1f}%"
    )

    # train_and_save 内部用 data[feature_cols]，构造带列名的 DataFrame
    feature_cols = [f"f{i}" for i in range(n_features)]
    X_df = pd.DataFrame(X_all, columns=feature_cols)
    model, scaler = train_and_save(X_df, y_all, feature_cols)

    # 保存报告
    report = {
        "trained_at": "2026-08-01",
        "train_cutoff": str(TRAIN_CUTOFF.date()),
        "pool": args.pool,
        "n_stocks": len([x for x in all_X if len(x) > 0]),
        "n_samples": len(X_all),
        "n_features": n_features,
        "horizon": args.horizon,
        "label_mean": float(y_all.mean()),
        "label_positive_ratio": float((y_all > 0).mean()),
        "model_path": str(
            Path(__file__).parent.parent / "data" / "ml_models" / "gradient_boost_v1.pkl"
        ),
    }
    Path("data/ml_gradient_boost_train_report.json").parent.mkdir(parents=True, exist_ok=True)
    with open("data/ml_gradient_boost_train_report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n模型已保存: {report['model_path']}")
    print(f"报告: data/ml_gradient_boost_train_report.json")


if __name__ == "__main__":
    main()
