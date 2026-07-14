"""Sanitizer: turn raw fleet telemetry into a whitelisted, pseudonymized snapshot.

Design goals (all enforced in code, not in a prompt):

  1. Whitelist, not blacklist. The agent-visible snapshot is built field by field
     from an allowlist. Anything the builder does not explicitly copy simply does
     not exist downstream. A new upstream field is invisible by default.

  2. Content-independent pseudonyms. Component identities are assigned positionally
     by role (exec-host-a/b/c, observer). Engines and any other detail survive only
     as counts. The raw identity is never copied, so even a real hostname in the raw
     pull cannot reach the output.

  3. Only counts, UTC timestamps, statuses, and generic classes survive. No paths,
     addresses, amounts, thresholds, or free identifiers.

  4. A tripwire that fails closed. Before anything is written, the fully built
     snapshot is scanned for forbidden tokens. A hit aborts the write. The safe
     failure is no output at all.

This module ships with only structural forbidden-token patterns (addresses, paths,
emails, key-shaped hex, currency-sign amounts). Operators add their own private
denylist locally via the WATCHTOWER_DENYLIST env var (newline-separated tokens);
none ship in this public extract.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_PATH = HERE / "fixtures" / "raw_sample.json"
SNAPSHOT_DIR = HERE / "snapshot"
SNAPSHOT_PATH = SNAPSHOT_DIR / "ops_snapshot.json"

SCHEMA = "watchtower-ops-v1"


class SanitizationError(Exception):
    """Raised when the forbidden-token screen trips. Nothing is written."""


# --- Forbidden-token screen ------------------------------------------------

# Structural patterns that indicate un-sanitized content leaking into a payload.
# These are generic on purpose: no venue, symbol, or strategy vocabulary ships in
# this public repo. The denylist for private tokens is loaded from the environment.
FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ipv4-address", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("filesystem-path", re.compile(r"(?:/[A-Za-z0-9_.\-]+){2,}")),
    ("email-address", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("key-shaped-hex", re.compile(r"\b[0-9a-fA-F]{16,}\b")),
    ("currency-amount", re.compile(r"[$€£¥]\s?\d")),
]


def _extra_denylist() -> list[str]:
    """Private token denylist, supplied by the operator, empty in this repo."""
    raw = os.environ.get("WATCHTOWER_DENYLIST", "")
    return [tok.strip().lower() for tok in raw.splitlines() if tok.strip()]


def scan_forbidden(blob: str) -> list[str]:
    """Return a list of human-readable hit descriptions. Empty means clean."""
    hits: list[str] = []
    for name, pattern in FORBIDDEN_PATTERNS:
        for match in pattern.findall(blob):
            hits.append(f"{name}: {match!r}")
    lowered = blob.lower()
    for token in _extra_denylist():
        if token in lowered:
            hits.append(f"denylist-token: {token!r}")
    return hits


def assert_clean(blob: str) -> None:
    """Fail closed: raise if the blob carries anything forbidden."""
    hits = scan_forbidden(blob)
    if hits:
        raise SanitizationError(
            "forbidden token(s) in payload; refusing to emit:\n  "
            + "\n  ".join(hits)
        )


# --- Pseudonym discipline --------------------------------------------------

def _assign_pseudonyms(components: list[dict]) -> list[str]:
    """Assign content-independent pseudonyms positionally by role.

    Execution components -> exec-host-a, exec-host-b, ... ; observers -> observer;
    anything else -> component-N. The raw id is never consulted.
    """
    pseudonyms: list[str] = []
    exec_index = 0
    for comp in components:
        role = comp.get("role")
        if role == "execution":
            pseudonyms.append(f"exec-host-{string.ascii_lowercase[exec_index]}")
            exec_index += 1
        elif role == "observer":
            pseudonyms.append("observer")
        else:
            pseudonyms.append(f"component-{len(pseudonyms) + 1}")
    return pseudonyms


def _allowed_pseudonyms(count: int, engine_total: int) -> set[str]:
    allowed = {"observer"}
    for letter in string.ascii_lowercase[:count]:
        allowed.add(f"exec-host-{letter}")
    for idx in range(1, engine_total + 1):
        allowed.add(f"engine-{idx}")
    for idx in range(1, count + 1):
        allowed.add(f"component-{idx}")
    return allowed


def assert_pseudonyms(snapshot: dict) -> None:
    """Defense in depth: every component label in the output must be a pseudonym."""
    comps = snapshot["fleet"]["components"]
    engine_total = snapshot["fleet"]["engines_configured_total"]
    allowed = _allowed_pseudonyms(len(comps), engine_total)
    labels = [c["component"] for c in comps]
    labels += [hb["component"] for hb in snapshot["heartbeats"]["per_component"]]
    labels += [ev["component"] for ev in snapshot["alerts"]["events"]]
    for label in labels:
        if label not in allowed:
            raise SanitizationError(f"non-pseudonym component label leaked: {label!r}")


# --- Whitelisted snapshot builder ------------------------------------------

def _age_seconds(captured_at: str, moment: str) -> int:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime.strptime(captured_at, fmt).replace(tzinfo=timezone.utc)
    b = datetime.strptime(moment, fmt).replace(tzinfo=timezone.utc)
    return int((a - b).total_seconds())


def _recovery_minutes(raised_at: str, recovered_at: str | None) -> int | None:
    if not recovered_at:
        return None
    return _age_seconds(recovered_at, raised_at) // 60


def build_snapshot(raw: dict) -> dict:
    """Pure transform: raw telemetry -> whitelisted, pseudonymized snapshot.

    Reads only known keys. Any extra field in the raw pull is dropped by omission.
    """
    captured_at = raw["captured_at"]
    comps_raw = raw["fleet"]["components"]
    pseudonyms = _assign_pseudonyms(comps_raw)

    components = []
    heartbeats = []
    engines_running_total = 0
    engines_configured_total = 0
    for label, comp in zip(pseudonyms, comps_raw):
        running = int(comp.get("engines_running", 0))
        configured = int(comp.get("engines_configured", 0))
        engines_running_total += running
        engines_configured_total += configured
        components.append(
            {
                "component": label,
                "role": comp["role"],
                "status": comp["status"],
                "engines_running": running,
                "engines_configured": configured,
                "uptime_hours": int(comp["uptime_seconds"]) // 3600,
            }
        )
        hb = comp["heartbeat"]
        expected = int(hb["expected"])
        received = int(hb["received"])
        heartbeats.append(
            {
                "component": label,
                "expected": expected,
                "received": received,
                "coverage_pct": round(received / expected * 100, 1) if expected else 0.0,
                "last_age_seconds": _age_seconds(captured_at, comp["last_heartbeat"]),
            }
        )

    recon_raw = raw["reconciliation"]
    runs = recon_raw["runs"]
    consecutive = 0
    for run in reversed(runs):
        if run["status"] == "ALL_VERIFIED":
            consecutive += 1
        else:
            break

    alerts_raw = raw["alerts"]
    events = []
    for ev in alerts_raw["events"]:
        events.append(
            {
                "alert_class": ev["alert_class"],
                "component": pseudonyms[ev["component_index"]],
                "severity": ev["severity"],
                "paged": bool(ev["paged"]),
                "raised_at": ev["raised_at"],
                "recovered_at": ev.get("recovered_at"),
                "recovery_minutes": _recovery_minutes(ev["raised_at"], ev.get("recovered_at")),
                "dedup_suppressed": int(ev.get("dedup_suppressed", 0)),
            }
        )
    paged_total = sum(1 for ev in events if ev["paged"])

    audit_raw = raw["audit"]
    by_class = [
        {"event_class": row["event_class"], "count": int(row["count"])}
        for row in audit_raw["by_class"]
    ]

    topo_raw = raw["topology"]
    watchdogs = [
        {"watchdog": w["watchdog"], "scope": w["scope"], "state": w["state"]}
        for w in topo_raw["watchdogs"]
    ]

    snapshot = {
        "snapshot_meta": {
            "generated_at": captured_at,
            "window_hours": 24,
            "schema": SCHEMA,
        },
        "fleet": {
            "components": components,
            "engines_running_total": engines_running_total,
            "engines_configured_total": engines_configured_total,
        },
        "heartbeats": {
            "window_hours": raw["heartbeats"]["window_hours"],
            "interval_seconds": raw["heartbeats"]["interval_seconds"],
            "per_component": heartbeats,
        },
        "reconciliation": {
            "window_hours": recon_raw["window_hours"],
            "runs": len(runs),
            "consecutive_verified": consecutive,
            "latest_status": runs[-1]["status"],
            "accounts_checked_per_run": recon_raw["accounts_per_run"],
            "last_run_at": runs[-1]["at"],
        },
        "alerts": {
            "window_hours": alerts_raw["window_hours"],
            "total": len(events),
            "paged": paged_total,
            "log_only": len(events) - paged_total,
            "events": events,
        },
        "audit": {
            "window_hours": audit_raw["window_hours"],
            "total_events": sum(row["count"] for row in by_class),
            "by_class": by_class,
        },
        "topology": {
            "components": len(components),
            "execution_hosts": sum(1 for c in components if c["role"] == "execution"),
            "observers": sum(1 for c in components if c["role"] == "observer"),
            "engines": engines_configured_total,
            "watchdogs": watchdogs,
        },
    }
    return snapshot


def sanitize(raw: dict) -> dict:
    """Build and screen a snapshot. Raises SanitizationError on any leak."""
    snapshot = build_snapshot(raw)
    assert_pseudonyms(snapshot)
    assert_clean(json.dumps(snapshot, ensure_ascii=False))
    return snapshot


def sanitize_file(raw_path: Path = RAW_PATH, out_path: Path = SNAPSHOT_PATH) -> dict:
    """Read raw, build the snapshot, screen it, and only then write.

    Fail-closed: if the screen trips, no file is written.
    """
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    snapshot = sanitize(raw)  # raises before any write on a dirty payload
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return snapshot


def main() -> int:
    try:
        snapshot = sanitize_file()
    except SanitizationError as exc:
        print("SANITIZATION SCREEN TRIPPED, wrote nothing:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    comps = snapshot["fleet"]["components"]
    print(f"snapshot written: {SNAPSHOT_PATH}")
    print(f"  components={len(comps)} engines={snapshot['fleet']['engines_configured_total']}"
          f" alerts={snapshot['alerts']['total']} audit={snapshot['audit']['total_events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
