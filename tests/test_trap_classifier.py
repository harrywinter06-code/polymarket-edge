# ruff: noqa: N806
"""Tests for the stdlib logistic-regression trap classifier."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from polymarket_edge import db
from polymarket_edge.trap_classifier import (
    FEATURE_ORDER,
    TrapFeatures,
    _auc_mann_whitney,
    featurize,
    fit_logreg,
    leave_one_out_cv,
    load_classifications_from_db,
)

# ---------------------------------------------------------------------------
# fit_logreg
# ---------------------------------------------------------------------------


def _feat(
    *,
    n_markets: int = 4,
    top_bps: float = 100.0,
    is_us_politics: int = 0,
    is_two_market: int = 0,
    augmented: int = 0,
) -> TrapFeatures:
    return TrapFeatures(
        n_markets=n_markets,
        top_of_book_gap_bps=top_bps,
        is_us_politics=is_us_politics,
        is_two_market=is_two_market,
        neg_risk_augmented=augmented,
    )


def test_fit_logreg_linearly_separable() -> None:
    """is_us_politics=1 ⇒ trap (y=1); is_us_politics=0 ⇒ non-trap (y=0).
    Perfect separation. Coefficient on is_us_politics must be positive and
    training-set accuracy at threshold 0.5 must be 100%."""
    X: list[TrapFeatures] = []
    y: list[int] = []
    for _ in range(15):
        X.append(_feat(is_us_politics=1, top_bps=80.0, n_markets=2, is_two_market=1))
        y.append(1)
    for _ in range(15):
        X.append(_feat(is_us_politics=0, top_bps=80.0, n_markets=8))
        y.append(0)

    model = fit_logreg(X, y, max_iter=5000, l2=0.001)
    assert model.coefs["is_us_politics"] > 0

    preds = [1 if model.predict_proba(f) >= 0.5 else 0 for f in X]
    assert preds == y


def test_fit_logreg_l2_prevents_blow_up() -> None:
    """All labels identical (degenerate). Without L2 the intercept would drift
    toward -inf; with L2 every coefficient stays bounded."""
    X = [_feat(top_bps=v, n_markets=2) for v in (50.0, 150.0, 75.0, 200.0, 30.0)]
    y = [0, 0, 0, 0, 0]
    model = fit_logreg(X, y, l2=0.01, max_iter=5000)
    for coef in model.coefs.values():
        assert abs(coef) < 50.0, f"coef blew up: {coef}"
    assert abs(model.intercept) < 50.0


def test_predict_proba_in_unit_interval() -> None:
    rng = random.Random(0)
    X = [
        _feat(
            n_markets=rng.randint(2, 30),
            top_bps=rng.uniform(50.0, 5000.0),
            is_us_politics=rng.randint(0, 1),
            is_two_market=rng.randint(0, 1),
            augmented=rng.randint(0, 1),
        )
        for _ in range(20)
    ]
    y = [rng.randint(0, 1) for _ in range(20)]
    model = fit_logreg(X, y, max_iter=500)
    # Probe at random inputs (not just training data).
    for _ in range(200):
        f = _feat(
            n_markets=rng.randint(2, 100),
            top_bps=rng.uniform(-1000.0, 10_000.0),
            is_us_politics=rng.randint(0, 1),
            is_two_market=rng.randint(0, 1),
            augmented=rng.randint(0, 1),
        )
        p = model.predict_proba(f)
        assert 0.0 <= p <= 1.0


def test_leave_one_out_cv_on_perfect_data() -> None:
    """Same separable construction; LOOCV AUC should be exactly 1.0."""
    X: list[TrapFeatures] = []
    y: list[int] = []
    for _ in range(10):
        X.append(_feat(is_us_politics=1, is_two_market=1, n_markets=2))
        y.append(1)
    for _ in range(10):
        X.append(_feat(is_us_politics=0, is_two_market=0, n_markets=10))
        y.append(0)

    result = leave_one_out_cv(X, y, max_iter=5000, l2=0.001)
    assert result.auc == 1.0
    assert result.confusion["fp"] == 0
    assert result.confusion["fn"] == 0


def test_leave_one_out_cv_on_noise() -> None:
    """Random labels uncorrelated with features → AUC near 0.5."""
    rng = random.Random(42)
    X = [
        _feat(
            n_markets=rng.randint(2, 20),
            top_bps=rng.uniform(50.0, 2000.0),
            is_us_politics=rng.randint(0, 1),
            is_two_market=rng.randint(0, 1),
        )
        for _ in range(30)
    ]
    y = [rng.randint(0, 1) for _ in range(30)]
    result = leave_one_out_cv(X, y, max_iter=2000, l2=0.1)
    assert 0.3 <= result.auc <= 0.7, f"AUC on random labels was {result.auc}, expected near 0.5"


def test_featurize_us_politics_one_hot() -> None:
    base_row = {
        "n_markets": 5,
        "top_of_book_gap": 0.0150,  # 150bps
        "neg_risk_augmented": 0,
    }

    for tag in ("Politics", "Elections", "US Election", "Midterms"):
        f = featurize({**base_row, "category_tag": tag})
        assert f.is_us_politics == 1, f"tag {tag!r} should map to is_us_politics=1"

    for tag in ("Sports", "Soccer", "Awards", "Business", None, ""):
        f = featurize({**base_row, "category_tag": tag})
        assert f.is_us_politics == 0, f"tag {tag!r} should map to is_us_politics=0"


def test_featurize_two_market_and_bps_scaling() -> None:
    row = {
        "n_markets": 2,
        "top_of_book_gap": 0.0230,
        "category_tag": "Politics",
        "neg_risk_augmented": 1,
    }
    f = featurize(row)
    assert f.is_two_market == 1
    assert f.is_us_politics == 1
    assert f.neg_risk_augmented == 1
    assert abs(f.top_of_book_gap_bps - 230.0) < 1e-9


def test_load_classifications_excludes_noise(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    db.init_schema(conn)

    rows = [
        ("scan-1", "ev1", "real", "Politics", 2, 0.0100, 0.0080, 0.0070, 50.0),
        ("scan-1", "ev2", "trap", "Politics", 2, 0.0150, -0.0500, -0.0700, 5.0),
        ("scan-1", "ev3", "noise", "Sports", 8, 0.0010, 0.0010, 0.0010, 1000.0),
        ("scan-1", "ev4", "marginal", "Elections", 3, 0.0080, 0.0070, 0.0030, 200.0),
    ]
    for scan_id, eid, verdict, tag, n_m, top, gs, gm, throttle in rows:
        conn.execute(
            """
            INSERT INTO microstructure_classifications
            (scan_id, event_id, event_slug, event_title, category_tag, n_markets,
             neg_risk_augmented, direction, top_of_book_gap, gap_at_small_size,
             gap_at_med_size, throttle_notional_usd, verdict, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id, eid, f"{eid}-slug", f"Title {eid}", tag, n_m,
                0, "sell_yes", top, gs, gm, throttle, verdict, "2026-05-21T00:00:00Z",
            ),
        )
    conn.commit()

    loaded = load_classifications_from_db(conn)
    assert len(loaded) == 3
    assert all(r["verdict"] != "noise" for r in loaded)
    assert {r["event_id"] for r in loaded} == {"ev1", "ev2", "ev4"}


def test_load_classifications_picks_latest_scan(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    db.init_schema(conn)

    common = (
        "ev",  # event_slug
        "Title",  # event_title
        "Politics",  # category_tag
        2,  # n_markets
        0,  # neg_risk_augmented
        "sell_yes",  # direction
        0.0100,  # top_of_book_gap
        -0.05,  # gap_at_small_size
        -0.07,  # gap_at_med_size
        50.0,  # throttle
        "trap",
    )

    # Older scan
    conn.execute(
        """
        INSERT INTO microstructure_classifications
        (scan_id, event_id, event_slug, event_title, category_tag, n_markets,
         neg_risk_augmented, direction, top_of_book_gap, gap_at_small_size,
         gap_at_med_size, throttle_notional_usd, verdict, classified_at)
        VALUES (?, 'old-ev', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("scan-old", *common, "2026-04-01T00:00:00Z"),
    )
    # Newer scan
    conn.execute(
        """
        INSERT INTO microstructure_classifications
        (scan_id, event_id, event_slug, event_title, category_tag, n_markets,
         neg_risk_augmented, direction, top_of_book_gap, gap_at_small_size,
         gap_at_med_size, throttle_notional_usd, verdict, classified_at)
        VALUES (?, 'new-ev', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("scan-new", *common, "2026-05-21T00:00:00Z"),
    )
    conn.commit()

    loaded = load_classifications_from_db(conn)
    assert {r["event_id"] for r in loaded} == {"new-ev"}


# ---------------------------------------------------------------------------
# AUC computation
# ---------------------------------------------------------------------------


def test_auc_perfect_separation() -> None:
    assert _auc_mann_whitney([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0


def test_auc_inverted_separation() -> None:
    assert _auc_mann_whitney([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0


def test_auc_ties_get_half_credit() -> None:
    # Both positives and both negatives tied at the same score => AUC = 0.5
    assert _auc_mann_whitney([0.5, 0.5, 0.5, 0.5], [0, 0, 1, 1]) == 0.5


def test_auc_one_class_only_returns_half() -> None:
    # No contrast — convention: 0.5.
    assert _auc_mann_whitney([0.1, 0.5, 0.9], [1, 1, 1]) == 0.5


def test_fit_logreg_consumes_featurize_output() -> None:
    """Integration: load rows-as-dicts, featurise, fit, predict — no surprises."""
    rows = [
        {"n_markets": 2, "top_of_book_gap": 0.0100, "category_tag": "Politics",
         "neg_risk_augmented": 0},
        {"n_markets": 48, "top_of_book_gap": 0.0150, "category_tag": "Soccer",
         "neg_risk_augmented": 0},
        {"n_markets": 2, "top_of_book_gap": 0.0070, "category_tag": "Elections",
         "neg_risk_augmented": 0},
        {"n_markets": 20, "top_of_book_gap": 0.4910, "category_tag": "Awards",
         "neg_risk_augmented": 0},
    ]
    X = [featurize(r) for r in rows]
    y = [1, 0, 1, 0]
    model = fit_logreg(X, y, max_iter=2000, l2=0.05)
    assert set(model.coefs.keys()) == set(FEATURE_ORDER)
    for f in X:
        p = model.predict_proba(f)
        assert 0.0 <= p <= 1.0


def test_load_classifications_empty_db_returns_empty_list(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    assert load_classifications_from_db(conn) == []


def test_load_classifications_respects_explicit_scan_id(tmp_path: Path) -> None:
    db_path = tmp_path / "two.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    common = (
        "ev", "Title", "Politics", 2, 0, "sell_yes",
        0.0100, -0.05, -0.07, 50.0, "trap",
    )
    conn.execute(
        """
        INSERT INTO microstructure_classifications
        (scan_id, event_id, event_slug, event_title, category_tag, n_markets,
         neg_risk_augmented, direction, top_of_book_gap, gap_at_small_size,
         gap_at_med_size, throttle_notional_usd, verdict, classified_at)
        VALUES (?, 'a', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("scan-A", *common, "2026-04-01T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO microstructure_classifications
        (scan_id, event_id, event_slug, event_title, category_tag, n_markets,
         neg_risk_augmented, direction, top_of_book_gap, gap_at_small_size,
         gap_at_med_size, throttle_notional_usd, verdict, classified_at)
        VALUES (?, 'b', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("scan-B", *common, "2026-05-01T00:00:00Z"),
    )
    conn.commit()

    a_rows = load_classifications_from_db(conn, scan_id="scan-A")
    b_rows = load_classifications_from_db(conn, scan_id="scan-B")
    assert [r["event_id"] for r in a_rows] == ["a"]
    assert [r["event_id"] for r in b_rows] == ["b"]


def test_fit_logreg_rejects_mismatched_lengths() -> None:
    import pytest
    with pytest.raises(ValueError):
        fit_logreg([_feat()], [0, 1])


def test_leave_one_out_cv_rejects_tiny_sample() -> None:
    import pytest
    with pytest.raises(ValueError):
        leave_one_out_cv([_feat(), _feat()], [0, 1])


def test_sqlite_row_factory_restored_after_load(tmp_path: Path) -> None:
    """Loader must not leak its row_factory choice back to the caller."""
    db_path = tmp_path / "rf.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    # db.connect sets row_factory = sqlite3.Row
    assert conn.row_factory is sqlite3.Row
    load_classifications_from_db(conn)
    assert conn.row_factory is sqlite3.Row
