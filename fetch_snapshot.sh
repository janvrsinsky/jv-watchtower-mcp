#!/usr/bin/env bash
#
# fetch_snapshot.sh - read-only telemetry pull (skeleton).
#
# This is the SHAPE of the host-side pull, included so the pipeline reads end to
# end. It is deliberately a skeleton: the public extract in this repo needs no
# live pull. It runs sanitize.py directly on fixtures/raw_sample.json (synthetic
# telemetry), so you can try the whole project without any hosts. Point this at
# your own fleet to produce a real raw pull instead of the fixture.
#
# Design contract (mirrors the private original):
#   * Read-only. Only a `tail` of operational logs plus host-local aggregation.
#     Nothing is written on the remote host and no configuration is touched.
#   * Sensitive streams (such as account reconciliation) are reduced to counts
#     and statuses ON THE HOST, so raw values never travel. Only timestamps and
#     counts leave the box.
#   * Output is a raw pull shaped like fixtures/raw_sample.json, which
#     sanitize.py then whitelists and pseudonymizes.
#
set -euo pipefail

# Space-separated hosts to pull from. Empty by default: supply your own.
HOSTS="${WATCHTOWER_HOSTS:-}"
# Read-only path to the operational telemetry log on each host (placeholder).
LOG_PATH="${WATCHTOWER_LOG_PATH:-/var/log/ops/telemetry.jsonl}"
# Trailing lines to tail (read-only).
TAIL_LINES="${WATCHTOWER_TAIL_LINES:-2000}"
# Where the assembled raw pull would land; sanitize.py reads this path.
OUT_PATH="${WATCHTOWER_RAW_OUT:-fixtures/raw_sample.json}"

if [[ -z "$HOSTS" ]]; then
  cat >&2 <<'MSG'
fetch_snapshot.sh is a skeleton and no hosts are configured.
The shipped pipeline needs no live pull. Run:

    python sanitize.py

which sanitizes the synthetic fixtures/raw_sample.json in place. Set
WATCHTOWER_HOSTS to wire this against your own read-only telemetry.
MSG
  exit 0
fi

# Per host, read-only: tail the telemetry log and aggregate on the host so that
# raw account values never leave it. `ssh` runs a single read-only command; adapt
# the remote aggregation to your own telemetry format before emitting OUT_PATH.
for host in $HOSTS; do
  ssh -o BatchMode=yes "$host" "tail -n ${TAIL_LINES} -- '${LOG_PATH}'" \
    || { echo "read-only tail failed for ${host}" >&2; exit 1; }
done > /dev/null

echo "Skeleton: wire host-side aggregation here to assemble ${OUT_PATH}." >&2
