"""Read-only MCP operations console over the sanitized fleet snapshot.

Six typed tools. Every one reads exclusively from the sanitized snapshot and passes
its own output through the forbidden-token screen on the way out (a second, independent
wall after the sanitizer). A screen hit raises instead of returning: two layers have to
fail silently for a single token to escape.

The tools cannot trade, change configuration, or read a single trade. The trading data
is absent from the snapshot, so a question like "what is the fleet trading?" has nothing
to retrieve. The refusal is structural, not a polite decline.

Run the server (needs fastmcp, see requirements.txt):

    python mcp_server.py

Importing this module does not require fastmcp: a tiny shim lets the pure tool functions
and the test harness run with the standard library alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from sanitize import SNAPSHOT_PATH, assert_clean

HERE = Path(__file__).resolve().parent
FIXTURE_SNAPSHOT = HERE / "fixtures" / "expected_snapshot.json"

try:  # fastmcp is only needed to actually serve; import stays optional for tests.
    from fastmcp import FastMCP

    _HAVE_FASTMCP = True
except Exception:  # pragma: no cover - exercised only without the dependency
    _HAVE_FASTMCP = False

    class FastMCP:  # minimal shim: keeps decorated functions directly callable
        def __init__(self, *args, **kwargs) -> None:
            pass

        def tool(self, fn=None, **kwargs):
            def wrap(func):
                return func

            return wrap if fn is None else wrap(fn)

        def run(self, *args, **kwargs):
            raise RuntimeError("fastmcp is not installed; pip install -r requirements.txt")


def _load_snapshot() -> dict:
    """Prefer a freshly sanitized snapshot; fall back to the shipped fixture."""
    path = SNAPSHOT_PATH if SNAPSHOT_PATH.exists() else FIXTURE_SNAPSHOT
    return json.loads(path.read_text(encoding="utf-8"))


SNAPSHOT = _load_snapshot()


def _screen(payload: dict) -> dict:
    """Second wall: re-screen every tool output. A hit raises, never returns."""
    assert_clean(json.dumps(payload, ensure_ascii=False))
    return payload


# --- Pure tool bodies (screened) -------------------------------------------

def tool_fleet_status() -> dict:
    fleet = SNAPSHOT["fleet"]
    return _screen(
        {
            "components": fleet["components"],
            "engines_running_total": fleet["engines_running_total"],
            "engines_configured_total": fleet["engines_configured_total"],
            "all_running": all(c["status"] == "running" for c in fleet["components"]),
            "generated_at": SNAPSHOT["snapshot_meta"]["generated_at"],
        }
    )


def tool_heartbeat_coverage() -> dict:
    hb = SNAPSHOT["heartbeats"]
    per = hb["per_component"]
    return _screen(
        {
            "window_hours": hb["window_hours"],
            "interval_seconds": hb["interval_seconds"],
            "per_component": per,
            "full_coverage": all(c["coverage_pct"] == 100.0 for c in per),
            "worst_last_age_seconds": max(c["last_age_seconds"] for c in per),
        }
    )


def tool_reconciliation_report() -> dict:
    return _screen(dict(SNAPSHOT["reconciliation"]))


def tool_alert_history() -> dict:
    return _screen(dict(SNAPSHOT["alerts"]))


def tool_audit_trail_summary() -> dict:
    return _screen(dict(SNAPSHOT["audit"]))


def tool_monitoring_topology() -> dict:
    return _screen(dict(SNAPSHOT["topology"]))


# Registry the test harness iterates over: name -> screened callable.
TOOLS = {
    "get_fleet_status": tool_fleet_status,
    "get_heartbeat_coverage": tool_heartbeat_coverage,
    "get_reconciliation_report": tool_reconciliation_report,
    "get_alert_history": tool_alert_history,
    "get_audit_trail_summary": tool_audit_trail_summary,
    "get_monitoring_topology": tool_monitoring_topology,
}


SYSTEM_PROMPT = """You are a read-only operations console over a monitored fleet.

Grounding rules:
- State only what a tool returned. Never infer numbers a tool did not report.
- Cite each fact with a component pseudonym and a UTC timestamp.
- Treat coverage gaps, incomplete reconciliations, and alerts without a recovery as
  findings, not footnotes.
- A daily review always runs the full protocol across all six tools. Never answer from
  a subset.

Hard scope boundary:
- You expose operational health only: fleet status, heartbeat coverage, reconciliation
  runs, alert history, audit volumes, monitoring topology.
- You cannot trade, change configuration, or read any position, balance, symbol, venue,
  or strategy. That data is absent from the snapshot by design.
- If asked what the fleet is trading, or for any private internal, refuse plainly: the
  console exposes operational telemetry only; that data is stripped in the data layer.
"""


# --- FastMCP registration --------------------------------------------------

mcp = FastMCP("watchtower")


@mcp.tool
def get_fleet_status() -> dict:
    """Per-component run status and engine counts across the fleet."""
    return tool_fleet_status()


@mcp.tool
def get_heartbeat_coverage() -> dict:
    """Heartbeat coverage per component over the monitoring window."""
    return tool_heartbeat_coverage()


@mcp.tool
def get_reconciliation_report() -> dict:
    """Account reconciliation run counts and consecutive verified streak."""
    return tool_reconciliation_report()


@mcp.tool
def get_alert_history() -> dict:
    """Alert events in the window: class, severity, paged, recovery timing."""
    return tool_alert_history()


@mcp.tool
def get_audit_trail_summary() -> dict:
    """Audit event volumes by generic class over the window."""
    return tool_audit_trail_summary()


@mcp.tool
def get_monitoring_topology() -> dict:
    """Component counts and watchdog arming state."""
    return tool_monitoring_topology()


if __name__ == "__main__":
    mcp.run()
