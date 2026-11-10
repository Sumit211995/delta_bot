"""Train an optional win/lose filter on the strategy's own historical signals.

  python train_model.py --days 1000

Uses a CHRONOLOGICAL split (first 70% train, last 30% test). The model is saved only if it improves the
out-of-sample win rate of the trades it would take, with at least 30 test trades kept. Even then, treat the
result as a hypothesis: confirm with forward testing before trusting it.
"""
import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")
import argparse
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

from backtest import fetch_history, run_backtest
from config import load_config
from delta_client import DeltaClient

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=1000)
ap.add_argument("--csv")
ap.add_argument("--threshold", type=float, default=0.55)
a = ap.parse_args()

cfg = load_config()
df = pd.read_csv(a.csv) if a.csv else fetch_history(DeltaClient(cfg.base_url), cfg.symbol, cfg.resolution,
                                                    cfg.period_seconds, a.days)
trades, _ = run_backtest(df, cfg)
if len(trades) < 150:
    raise SystemExit(f"Only {len(trades)} trades: not enough data to train a meaningful model (need ~150+).")

X = np.array(trades["features"].tolist(), dtype=float)
y = trades["win"].values
ok = ~np.isnan(X).any(axis=1)
X, y = X[ok], y[ok]
cut = int(len(X) * 0.7)
Xtr, Xte, ytr, yte = X[:cut], X[cut:], y[:cut], y[cut:]

model = GradientBoostingClassifier(n_estimators=100, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)
model.fit(Xtr, ytr)
p = model.predict_proba(Xte)[:, 1]
keep = p >= a.threshold
base = yte.mean()
filt = yte[keep].mean() if keep.sum() else float("nan")
print(f"test trades={len(yte)} baseline win rate={base:.1%} | filtered kept={keep.sum()} win rate={filt:.1%}")

if keep.sum() >= 30 and filt > base + 0.03:
    os.makedirs("models", exist_ok=True)
    model.fit(X, y)  # refit on all data for deployment
    joblib.dump(model, "models/filter.joblib")
    print("Saved models/filter.joblib. Set ML_MODEL_PATH=models/filter.joblib and re-run forward tests.")
else:
    print("No reliable improvement out-of-sample. Model NOT saved (this is the common, honest outcome).")
