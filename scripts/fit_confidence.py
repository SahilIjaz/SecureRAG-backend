"""
Fit the pre-generation confidence coefficients (b0..b3) offline.

Input : the labelled CSV from  scripts/evaluate_rag.py --csv ...
Model : S = sigmoid(b0 + b1·top_rerank + b2·gap + b3·coverage)
        (the same formula as app/services/confidence.py:compute_pregen_confidence)
Output: fitted coefficients, held-out calibration (Brier, ECE), a reliability
        curve, a risk–coverage curve, and the refuse threshold that meets
        --target precision among answered questions. Paste the printed values
        into app/config.py (RAG_CONF_B0..B3) and set the dashboard
        confidenceThreshold default from the printed 0-100 threshold.

scikit-learn / matplotlib are dev-only (requirements-dev.txt); the runtime
only needs the four fitted floats.

Usage:
    PYTHONPATH=. ./venv/Scripts/python.exe scripts/fit_confidence.py \
        scripts/eval_results/e3.csv --target 0.9
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import train_test_split

FEATURES = ["top_rerank", "gap", "coverage"]


def expected_calibration_error(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Weighted mean |observed frequency − mean predicted probability| per bin."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            ece += mask.mean() * abs(y_true[mask].mean() - p[mask].mean())
    return float(ece)


def risk_coverage(y_true: np.ndarray, p: np.ndarray) -> pd.DataFrame:
    """For every threshold t: coverage = share answered (p >= t), precision =
    share of those answers that are good. Higher t -> lower coverage, higher precision."""
    rows = []
    for t in np.unique(np.round(p, 4)):
        answered = p >= t
        if answered.sum() == 0:
            continue
        rows.append({"threshold": float(t), "coverage": float(answered.mean()),
                     "precision": float(y_true[answered].mean())})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="labelled CSV from evaluate_rag.py --csv")
    ap.add_argument("--target", type=float, default=0.9,
                    help="required precision among answered questions (wrong-answer policy)")
    ap.add_argument("--test-size", type=float, default=0.3, help="held-out share, never used for fitting")
    ap.add_argument("--C", type=float, default=1.0, help="inverse L2 strength (smaller = stronger)")
    ap.add_argument("--drop-gap", action="store_true", help="fit without the gap feature")
    ap.add_argument("--out", default="scripts/eval_results", help="where to save the plots")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    feats = [f for f in FEATURES if not (args.drop_gap and f == "gap")]
    X, y = df[feats].to_numpy(float), df["label"].to_numpy(int)
    print(f"Rows: {len(df)}   good: {int(y.sum())}   bad: {int(len(y) - y.sum())}")
    if len(np.unique(y)) < 2 or min(y.sum(), len(y) - y.sum()) < 10:
        sys.exit("Need at least 10 examples of each class (good and bad) to fit — collect more data.")

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=args.test_size, stratify=y, random_state=args.seed)
    model = LogisticRegression(C=args.C, max_iter=1000).fit(X_tr, y_tr)
    p_te = model.predict_proba(X_te)[:, 1]

    b0 = float(model.intercept_[0])
    coefs = dict(zip(feats, model.coef_[0].astype(float)))
    print("\nFitted coefficients (paste into app/config.py):")
    print(f"  RAG_CONF_B0: float = {b0:.4f}")
    for i, name in enumerate(FEATURES, start=1):
        print(f"  RAG_CONF_B{i}: float = {coefs.get(name, 0.0):.4f}   # {name}"
              + ("   (dropped -> 0)" if name not in coefs else ""))
    if "gap" in coefs and coefs["gap"] < 0:
        print("  note: gap coefficient is negative — consider --drop-gap (near-duplicate chunks make a small gap a good sign)")

    brier = brier_score_loss(y_te, p_te)
    ece = expected_calibration_error(y_te, p_te)
    print(f"\nHeld-out ({len(y_te)} rows): Brier = {brier:.4f}   ECE = {ece:.4f}")

    rc = risk_coverage(y_te, p_te)
    ok = rc[rc["precision"] >= args.target]
    print(f"\nWrong-answer policy: precision >= {args.target:.0%} among answered")
    if ok.empty:
        print("  No threshold reaches the target on the held-out set — improve retrieval or relax the target.")
    else:
        best = ok.sort_values("threshold").iloc[0]  # lowest threshold = highest coverage that still meets target
        print(f"  probability threshold = {best.threshold:.3f}  -> confidenceThreshold ~ {round(100 * best.threshold)}")
        print(f"  answers {best.coverage:.0%} of questions, {best.precision:.0%} of those are good")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)

        frac_pos, mean_pred = calibration_curve(y_te, p_te, n_bins=8, strategy="quantile")
        fig, ax = plt.subplots(figsize=(4.5, 4.5))
        ax.plot([0, 1], [0, 1], "--", color="grey", label="perfect")
        ax.plot(mean_pred, frac_pos, "o-", label=f"model (ECE {ece:.3f})")
        ax.set_xlabel("predicted confidence")
        ax.set_ylabel("observed fraction good")
        ax.set_title("Reliability curve (held-out)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / "reliability_curve.png", dpi=150)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(4.5, 4.5))
        ax.plot(rc["coverage"], rc["precision"])
        ax.axhline(args.target, ls="--", color="grey")
        ax.set_xlabel("coverage (share of questions answered)")
        ax.set_ylabel("precision (share of answers good)")
        ax.set_title("Risk–coverage (held-out)")
        fig.tight_layout()
        fig.savefig(out / "risk_coverage.png", dpi=150)
        plt.close(fig)
        print(f"\nPlots saved to {out}/reliability_curve.png and risk_coverage.png")
    except ImportError:
        print("\n(matplotlib not installed — skipped plots)")


if __name__ == "__main__":
    main()
