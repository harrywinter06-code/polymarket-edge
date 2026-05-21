# ruff: noqa: N806
"""Train and evaluate the trap classifier on the latest microstructure scan.

Loads the latest scan from microstructure_classifications, drops 'noise' rows,
fits a stdlib logistic regression, and reports LOOCV AUC plus a confusion
matrix at p=0.5. Prints the 3 highest-trap-probability events and the 3
lowest, with their actual verdicts.

Usage:
    PYTHONPATH=src python scripts/trap_classifier_train.py
    PYTHONPATH=src python scripts/trap_classifier_train.py --db polymarket_edge.db
    PYTHONPATH=src python scripts/trap_classifier_train.py --scan-id <hex>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polymarket_edge import db  # noqa: E402
from polymarket_edge.trap_classifier import (  # noqa: E402
    featurize,
    fit_logreg,
    leave_one_out_cv,
    load_classifications_from_db,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=Path("polymarket_edge.db"),
        help="SQLite DB path (default: ./polymarket_edge.db)",
    )
    parser.add_argument(
        "--scan-id", type=str, default=None,
        help="Specific scan_id to train on (default: latest scan).",
    )
    parser.add_argument(
        "--l2", type=float, default=0.05,
        help="L2 regularisation strength (default: 0.05, sane on n~19).",
    )
    parser.add_argument(
        "--max-iter", type=int, default=5000,
        help="Max gradient-descent iterations (default: 5000).",
    )
    parser.add_argument(
        "--learning-rate", type=float, default=0.5,
        help="GD learning rate (default: 0.5).",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        sys.exit(2)

    conn = db.connect(args.db)
    rows = load_classifications_from_db(conn, scan_id=args.scan_id)
    if not rows:
        print(
            f"No classifications found in {args.db}"
            + (f" for scan_id={args.scan_id}" if args.scan_id else ""),
            file=sys.stderr,
        )
        sys.exit(1)

    X = [featurize(r) for r in rows]
    y = [1 if r["verdict"] == "trap" else 0 for r in rows]
    n = len(X)
    n_traps = sum(y)
    base_rate = n_traps / n

    print("=" * 80)
    print("Trap classifier — logistic regression, LOOCV evaluation")
    print("=" * 80)
    print(f"Source DB:     {args.db}")
    print(f"Scan ID:       {rows[0]['scan_id']}")
    print(f"N samples:     {n}  (verdict != 'noise')")
    print(f"N traps:       {n_traps}  (base rate {base_rate * 100:.1f}%)")
    print(f"L2:            {args.l2}   learning_rate: {args.learning_rate}   "
          f"max_iter: {args.max_iter}")

    fit_kwargs = {
        "l2": args.l2,
        "max_iter": args.max_iter,
        "learning_rate": args.learning_rate,
    }
    result = leave_one_out_cv(X, y, **fit_kwargs)

    print()
    print(f"LOOCV ROC AUC:       {result.auc:.3f}")
    print(f"Accuracy @ p=0.5:    {result.accuracy_at_threshold_05:.3f}")
    print()
    print("Confusion matrix @ p=0.5 (LOOCV held-out predictions):")
    cm = result.confusion
    print("                  pred trap   pred non-trap")
    print(f"  actual trap     {cm['tp']:>9d}   {cm['fn']:>13d}")
    print(f"  actual non-trap {cm['fp']:>9d}   {cm['tn']:>13d}")

    print()
    print("Feature coefficients (full-data fit, sorted by |coef|):")
    for name, coef in result.feature_importance_ordered:
        sign = "+" if coef >= 0 else "-"
        direction = "increases" if coef >= 0 else "decreases"
        print(f"  {name:<22} {sign}{abs(coef):>10.4f}   ({direction} trap prob)")

    # Full-data model for predict_proba on every sample.
    model = fit_logreg(X, y, **fit_kwargs)
    scored = [
        (model.predict_proba(f), rows[i]["event_slug"], rows[i]["verdict"],
         rows[i]["category_tag"], rows[i]["n_markets"])
        for i, f in enumerate(X)
    ]
    scored.sort(key=lambda t: -t[0])

    print()
    print("Top 3 highest trap-probability events (full-data fit):")
    for prob, slug, verdict, cat, n_markets in scored[:3]:
        print(f"  p_trap={prob:.3f}  actual={verdict:<8}  "
              f"cat={cat:<14}  n={n_markets:>2}  {slug}")

    print()
    print("Top 3 lowest trap-probability events (full-data fit):")
    for prob, slug, verdict, cat, n_markets in scored[-3:][::-1]:
        print(f"  p_trap={prob:.3f}  actual={verdict:<8}  "
              f"cat={cat:<14}  n={n_markets:>2}  {slug}")

    print()
    print("Model intercept:", f"{model.intercept:.4f}")
    print(
        "Caveat: n=" + str(n) + " is far below the threshold where LOOCV AUC is "
        "a stable estimator; treat this as scaffolding."
    )


if __name__ == "__main__":
    main()
