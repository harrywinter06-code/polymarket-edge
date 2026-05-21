"""Cross-venue case study: Polymarket Fed-rate-cut market vs Hyperliquid BTC perp.

Pair:
  PM:  "Will no Fed rate cuts happen in 2026?" YES token (from event
       `how-many-fed-rate-cuts-in-2026`, the only currently-active, high-volume,
       negRisk-tagged Fed-decision event on Polymarket as of build date).
  HL:  BTC perpetual mark (close-of-hourly-candle).

Thesis (steelmanned against the obvious failure mode that BTC is dominated by
non-macro flow): a Fed easing surprise should propagate to crypto via the
risk-on channel. PM YES on "no cuts" is the cleanest single-token proxy for the
hawkish surprise; if it rises, the joint probability of cuts in 2026 falls, and
the risk-on bid for BTC should weaken. So we expect *negative* correlation
between PM probability change and BTC log-return at lag 0 and possibly +1.

Run: PYTHONPATH=src uv run python scripts/cross_venue_case.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from polymarket_edge.cross_venue import (  # noqa: E402
    align_series,
    compute_lead_lag,
    fetch_hl_mark_history,
    fetch_pm_price_history,
    insert_aligned_rows,
)
from polymarket_edge.db import connect, init_schema  # noqa: E402

PM_TOKEN_ID = (
    "12403602920039269077597917340921667997547115084613238528792639013246536343316"
)
PM_LABEL = "how-many-fed-rate-cuts-in-2026 :: Will no Fed rate cuts happen in 2026? (YES)"
HL_COIN = "BTC"
DAYS = 30
BUCKET_MINUTES = 720  # 12h, matching CLOB's worst-case resolved-market floor
MAX_LAG_BUCKETS = 4   # +/- 2 days at 12h granularity
DB_PATH = ROOT / "polymarket_edge.db"


def main() -> None:
    print(f"Cross-venue case study  ({datetime.now(UTC).isoformat()})")
    print(f"  PM: {PM_LABEL}")
    print(f"  HL: {HL_COIN} perp")
    print(f"  window: {DAYS} days, bucket={BUCKET_MINUTES}min, "
          f"max_lag=+/-{MAX_LAG_BUCKETS} buckets")
    print()

    pm, hl = asyncio.run(_fetch_both())
    print(f"Fetched: PM points={len(pm)}, HL candles={len(hl)}")
    if not pm or not hl:
        print("ABORT: empty leg.")
        return

    rows = align_series(pm, hl, bucket_minutes=BUCKET_MINUTES)
    print(f"Aligned 12h buckets with both legs present: {len(rows)}")
    if len(rows) < 5:
        print("ABORT: too few aligned buckets to correlate.")
        return

    first = datetime.fromtimestamp(rows[0].t_ms / 1000, UTC).isoformat()
    last = datetime.fromtimestamp(rows[-1].t_ms / 1000, UTC).isoformat()
    print(f"Window: {first} -> {last}")
    print(f"PM range: {min(r.pm_price for r in rows):.4f} -> "
          f"{max(r.pm_price for r in rows):.4f}")
    print(f"HL range: {min(r.hl_mark for r in rows):.1f} -> "
          f"{max(r.hl_mark for r in rows):.1f}")
    print()

    lags = compute_lead_lag(rows, max_lag_buckets=MAX_LAG_BUCKETS)
    print("Lead-lag (Pearson r between pm_delta and hl_log_return):")
    print(f"  {'lag (12h)':>10} | {'meaning':<30} | {'corr':>10}")
    print(f"  {'-' * 10} | {'-' * 30} | {'-' * 10}")
    for lag in sorted(lags):
        if lag < 0:
            meaning = f"HL leads PM by {-lag}"
        elif lag == 0:
            meaning = "contemporaneous"
        else:
            meaning = f"PM leads HL by {lag}"
        v = lags[lag]
        v_str = "nan" if v != v else f"{v:+.4f}"
        print(f"  {lag:>10d} | {meaning:<30} | {v_str:>10}")
    print()

    finite = {k: v for k, v in lags.items() if v == v}
    if finite:
        best_lag = max(finite, key=lambda k: abs(finite[k]))
        print(f"Headline: lag=0 r={finite.get(0, float('nan')):+.4f}, "
              f"best |r| at lag={best_lag} ({finite[best_lag]:+.4f})")

    fetched_at = datetime.now(UTC).isoformat()
    with connect(DB_PATH) as conn:
        init_schema(conn)
        n = insert_aligned_rows(
            conn,
            pm_token_id=PM_TOKEN_ID,
            hl_coin=HL_COIN,
            rows=rows,
            fetched_at=fetched_at,
        )
        conn.commit()
    print(f"\nPersisted {n} rows to cross_venue_aligned.")


async def _fetch_both() -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    return await asyncio.gather(
        fetch_pm_price_history(PM_TOKEN_ID, days=DAYS),
        fetch_hl_mark_history(HL_COIN, days=DAYS),
    )


if __name__ == "__main__":
    main()
