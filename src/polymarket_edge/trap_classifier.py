# ruff: noqa: N803, N806
"""Toy logistic-regression trap classifier on microstructure_classifications.

The descriptive MICROSTRUCTURE.md finding says "63% of detector-flagged events
are traps and concentration is in 2-market US politics". A founder asking
"given a new flagged event, what is its trap probability?" needs a model, not a
statistic. This module fits a tiny stdlib-only logistic regression with batch
gradient descent and L2 regularisation, and evaluates it under leave-one-out
cross-validation. With n~19 the model is scaffolding, not deployable
production — but the methodology (features, fit, LOOCV AUC, calibration on a
held-out fold) is what scales when more scans accumulate.

Features (see TrapFeatures):
  - n_markets:                event-level market count
  - top_of_book_gap_bps:      top-of-book gap in basis points (the detector's
                              own signal magnitude)
  - is_us_politics:           one-hot for category_tag in
                              {Politics, Elections, US Election, Midterms}
  - is_two_market:            n_markets == 2 (the mechanical trap shape)
  - neg_risk_augmented:       1 if the event is augmented (rare; informative if
                              augmented events have different liquidity)

Stdlib only. No sklearn, no scipy, no numpy.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

_US_POLITICS_TAGS = frozenset({"Politics", "Elections", "US Election", "Midterms"})

FEATURE_ORDER: tuple[str, ...] = (
    "n_markets",
    "top_of_book_gap_bps",
    "is_us_politics",
    "is_two_market",
    "neg_risk_augmented",
)


@dataclass(frozen=True, slots=True)
class TrapFeatures:
    n_markets: int
    top_of_book_gap_bps: float
    is_us_politics: int
    is_two_market: int
    neg_risk_augmented: int

    def as_vector(self) -> tuple[float, ...]:
        return (
            float(self.n_markets),
            float(self.top_of_book_gap_bps),
            float(self.is_us_politics),
            float(self.is_two_market),
            float(self.neg_risk_augmented),
        )


@dataclass(frozen=True, slots=True)
class TrapModel:
    intercept: float
    coefs: dict[str, float]
    feature_order: tuple[str, ...]
    n_train: int

    def predict_proba(self, features: TrapFeatures) -> float:
        """Probability this event is a trap (verdict == 'trap')."""
        z = self.intercept
        vec = features.as_vector()
        for name, x in zip(self.feature_order, vec, strict=True):
            z += self.coefs[name] * x
        return _sigmoid(z)


@dataclass(frozen=True, slots=True)
class LooCVResult:
    n_samples: int
    n_traps: int
    auc: float
    accuracy_at_threshold_05: float
    confusion: dict[str, int]
    feature_coefs: dict[str, float]
    feature_importance_ordered: list[tuple[str, float]]


# ---------------------------------------------------------------------------
# Featurisation
# ---------------------------------------------------------------------------


def featurize(row: dict[str, Any]) -> TrapFeatures:
    """Convert a microstructure_classifications row (dict-like) to TrapFeatures."""
    n_markets = int(row["n_markets"])
    top_of_book_gap = float(row["top_of_book_gap"])  # fractional, e.g. 0.0150
    top_bps = top_of_book_gap * 10_000.0
    tag = row.get("category_tag") or ""
    is_us_politics = 1 if tag in _US_POLITICS_TAGS else 0
    is_two_market = 1 if n_markets == 2 else 0
    augmented = int(row.get("neg_risk_augmented") or 0)
    return TrapFeatures(
        n_markets=n_markets,
        top_of_book_gap_bps=top_bps,
        is_us_politics=is_us_politics,
        is_two_market=is_two_market,
        neg_risk_augmented=1 if augmented else 0,
    )


# ---------------------------------------------------------------------------
# Logistic regression: batch GD with L2
# ---------------------------------------------------------------------------


def _sigmoid(z: float) -> float:
    # Numerically stable sigmoid.
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _standardize(
    X: list[tuple[float, ...]],
) -> tuple[list[tuple[float, ...]], tuple[float, ...], tuple[float, ...]]:
    """Per-column zero-mean unit-variance; constant columns left as zeros."""
    if not X:
        return [], (), ()
    n_features = len(X[0])
    n = len(X)
    means = [sum(row[j] for row in X) / n for j in range(n_features)]
    stds: list[float] = []
    for j in range(n_features):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        std = math.sqrt(var) if var > 0 else 0.0
        stds.append(std)
    scaled: list[tuple[float, ...]] = []
    for row in X:
        scaled.append(
            tuple(
                ((row[j] - means[j]) / stds[j]) if stds[j] > 0 else 0.0
                for j in range(n_features)
            )
        )
    return scaled, tuple(means), tuple(stds)


def fit_logreg(
    X: list[TrapFeatures],
    y: list[int],
    *,
    learning_rate: float = 0.5,
    max_iter: int = 2000,
    tol: float = 1e-7,
    l2: float = 0.01,
) -> TrapModel:
    """Batch gradient descent on logistic loss with L2 regularisation.

    The features are standardised internally so the optimiser converges in a
    bounded number of iterations regardless of input scale (top_of_book_gap_bps
    spans orders of magnitude vs the {0,1} one-hots). Coefficients are
    transformed back to the original feature scale before returning, so callers
    can plug them straight into predict_proba on raw TrapFeatures.
    """
    if len(X) != len(y):
        raise ValueError(f"X and y length mismatch: {len(X)} != {len(y)}")
    if not X:
        raise ValueError("cannot fit on empty data")
    if not all(yi in (0, 1) for yi in y):
        raise ValueError("y must be a list of {0, 1}")

    raw = [f.as_vector() for f in X]
    scaled, means, stds = _standardize(raw)
    n = len(scaled)
    n_features = len(FEATURE_ORDER)

    w = [0.0] * n_features
    b = 0.0
    prev_loss = float("inf")

    for _ in range(max_iter):
        # Forward pass.
        zs = [b + sum(w[j] * row[j] for j in range(n_features)) for row in scaled]
        ps = [_sigmoid(z) for z in zs]

        # Loss for convergence check (mean log-loss + L2 on w only).
        loss = 0.0
        for yi, pi in zip(y, ps, strict=True):
            pi_c = min(max(pi, 1e-15), 1.0 - 1e-15)
            loss -= yi * math.log(pi_c) + (1 - yi) * math.log(1.0 - pi_c)
        loss /= n
        loss += 0.5 * l2 * sum(wj * wj for wj in w)

        if abs(prev_loss - loss) < tol:
            break
        prev_loss = loss

        # Gradients.
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for row, yi, pi in zip(scaled, y, ps, strict=True):
            diff = pi - yi
            grad_b += diff
            for j in range(n_features):
                grad_w[j] += diff * row[j]
        grad_b /= n
        for j in range(n_features):
            grad_w[j] = grad_w[j] / n + l2 * w[j]

        b -= learning_rate * grad_b
        for j in range(n_features):
            w[j] -= learning_rate * grad_w[j]

    # Map standardised coefs back to raw-feature coefs.
    # z = b + sum_j w_j * (x_j - mean_j) / std_j
    #   = (b - sum_j w_j * mean_j / std_j) + sum_j (w_j / std_j) * x_j
    raw_w: list[float] = []
    raw_b = b
    for j in range(n_features):
        if stds[j] > 0:
            raw_w.append(w[j] / stds[j])
            raw_b -= w[j] * means[j] / stds[j]
        else:
            raw_w.append(0.0)

    coefs = dict(zip(FEATURE_ORDER, raw_w, strict=True))
    return TrapModel(
        intercept=raw_b,
        coefs=coefs,
        feature_order=FEATURE_ORDER,
        n_train=n,
    )


# ---------------------------------------------------------------------------
# Leave-one-out cross-validation
# ---------------------------------------------------------------------------


def _auc_mann_whitney(scores: list[float], labels: list[int]) -> float:
    """ROC AUC via the Mann-Whitney U formulation with mid-rank tie handling.

    AUC = P(score_positive > score_negative) + 0.5 * P(score_positive == score_negative)

    Equivalent to:
       (sum_of_midranks_of_positives - n_pos*(n_pos+1)/2) / (n_pos * n_neg)
    """
    n = len(scores)
    if n != len(labels):
        raise ValueError("scores and labels length mismatch")
    n_pos = sum(labels)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        # AUC undefined (no contrast). Convention: return 0.5 (random) so the
        # number is plottable; caller should report n_pos/n_neg alongside.
        return 0.5

    # Sort by score ascending, assign mid-ranks (1-based).
    indexed = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[indexed[j + 1]] == scores[indexed[i]]:
            j += 1
        # Tied group is indexed[i..j], inclusive. Mid-rank.
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1

    sum_ranks_pos = sum(r for r, lab in zip(ranks, labels, strict=True) if lab == 1)
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc


def leave_one_out_cv(
    X: list[TrapFeatures],
    y: list[int],
    *,
    shuffle_labels: bool = False,
    shuffle_seed: int = 0,
    **fit_kwargs: Any,
) -> LooCVResult:
    """LOOCV: fit on N-1, predict the held-out sample, then summarise.

    Pass ``shuffle_labels=True`` to get a negative-control AUC — the same
    pipeline with the y vector permuted, which should produce AUC ~ 0.5 if
    the model has no spurious-fitting capacity. The gap between the real AUC
    and the shuffled-label AUC is the actual signal from the features (and
    must be reported alongside the real AUC at low N to be honest).
    """
    n = len(X)
    if n != len(y):
        raise ValueError("X and y length mismatch")
    if n < 3:
        raise ValueError(f"need at least 3 samples for LOOCV, got {n}")

    if shuffle_labels:
        import random as _random
        rng = _random.Random(shuffle_seed)
        y = list(y)
        rng.shuffle(y)

    held_out_scores: list[float] = []
    held_out_labels: list[int] = []
    for i in range(n):
        X_train = [X[j] for j in range(n) if j != i]
        y_train = [y[j] for j in range(n) if j != i]
        model = fit_logreg(X_train, y_train, **fit_kwargs)
        held_out_scores.append(model.predict_proba(X[i]))
        held_out_labels.append(y[i])

    auc = _auc_mann_whitney(held_out_scores, held_out_labels)

    tp = fp = tn = fn = 0
    for score, label in zip(held_out_scores, held_out_labels, strict=True):
        pred = 1 if score >= 0.5 else 0
        if pred == 1 and label == 1:
            tp += 1
        elif pred == 1 and label == 0:
            fp += 1
        elif pred == 0 and label == 0:
            tn += 1
        else:
            fn += 1
    accuracy = (tp + tn) / n

    # Full-data fit for the deployable coefficients.
    full_model = fit_logreg(X, y, **fit_kwargs)
    importance = sorted(
        full_model.coefs.items(),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )

    return LooCVResult(
        n_samples=n,
        n_traps=sum(y),
        auc=auc,
        accuracy_at_threshold_05=accuracy,
        confusion={"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        feature_coefs=dict(full_model.coefs),
        feature_importance_ordered=importance,
    )


# ---------------------------------------------------------------------------
# DB loader
# ---------------------------------------------------------------------------


def load_classifications_from_db(
    conn: sqlite3.Connection,
    *,
    scan_id: str | None = None,
    pool_scans: bool = False,
) -> list[dict[str, Any]]:
    """Return rows from microstructure_classifications, excluding verdict='noise'.

    Mode selection (mutually exclusive):
      - ``pool_scans=True``: aggregate across every scan_id, deduping by
        event_id with the latest classification (by ``classified_at`` then
        ``id``) winning. This is the right loader once the daily cron has
        accumulated many scans — features per event are mostly static, so
        seeing the same event twice gives the same row; pooling expands N
        by counting each unique event once.
      - ``scan_id=<str>``: filter to exactly that scan.
      - default (both unset): pick the most recent scan by max(classified_at)
        and return only that scan's rows.
    """
    if pool_scans and scan_id is not None:
        raise ValueError("pass either pool_scans=True or scan_id, not both")

    prev_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        if pool_scans:
            # For each event_id, take the row from the latest scan
            # (classified_at DESC, id DESC as tiebreaker).
            rows = conn.execute(
                """
                SELECT *
                FROM microstructure_classifications c
                WHERE verdict != 'noise'
                  AND id = (
                    SELECT c2.id
                    FROM microstructure_classifications c2
                    WHERE c2.event_id = c.event_id
                      AND c2.verdict != 'noise'
                    ORDER BY c2.classified_at DESC, c2.id DESC
                    LIMIT 1
                  )
                ORDER BY id
                """
            ).fetchall()
            return [dict(r) for r in rows]

        if scan_id is None:
            row = conn.execute(
                """
                SELECT scan_id
                FROM microstructure_classifications
                GROUP BY scan_id
                ORDER BY MAX(classified_at) DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return []
            scan_id = row["scan_id"]
        rows = conn.execute(
            """
            SELECT *
            FROM microstructure_classifications
            WHERE scan_id = ? AND verdict != 'noise'
            ORDER BY id
            """,
            (scan_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.row_factory = prev_factory
