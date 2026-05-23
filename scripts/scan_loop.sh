#!/usr/bin/env bash
# scripts/scan_loop.sh — accumulate microstructure_classifications by running
# the CLI scan on a fixed cadence. Designed to run unattended on a VPS.
#
# Usage:
#   bash scripts/scan_loop.sh                    # 48 scans, 30 min apart (~24h)
#   bash scripts/scan_loop.sh <interval_s> <n>   # custom cadence + count
#
# Writes each scan's stdout to scan_loop.log alongside the DB. Each scan
# inserts ~10-30 rows depending on current market activity. The whole loop
# is restart-safe: dedupe is by (scan_id, event_id) so re-running adds rather
# than overwriting.

set -uo pipefail

INTERVAL_S="${1:-1800}"      # default 30 min
N_SCANS="${2:-48}"           # default 48 = ~24h at 30-min cadence
DB="${DB:-/opt/polymarket-edge/polymarket_edge.db}"
LOG="${LOG:-/opt/polymarket-edge/scan_loop.log}"

cd "$(dirname "${DB}")"
exec >>"${LOG}" 2>&1
echo
echo "==== scan_loop start $(date -u +%Y-%m-%dT%H:%M:%SZ) — ${N_SCANS} scans, ${INTERVAL_S}s apart"

for i in $(seq 1 "${N_SCANS}"); do
    echo
    echo "---- scan ${i}/${N_SCANS} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    uv run polymarket-edge microstructure-scan \
        --db-path "${DB}" \
        --max-events 500 \
        --small-size-usd 50 \
        --med-size-usd 500 \
        --fee-buffer 0.005 \
        2>&1 | tail -40
    # Don't sleep after the last scan.
    if [ "${i}" -lt "${N_SCANS}" ]; then
        sleep "${INTERVAL_S}"
    fi
done

echo
echo "==== scan_loop done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
