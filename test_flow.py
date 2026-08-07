"""Pre-record frame check. Runs offline with the standard library only.

It is the gate that must pass before a single frame is captured:

  1. The sanitized snapshot loads, passes the forbidden-token screen, and every
     component label in it is a pseudonym.
  2. sanitize(raw_sample) reproduces the expected snapshot exactly (transform is
     faithful and deterministic).
  3. Every one of the six tool outputs is leak-scanned; a hit fails the run.
  4. The agent system prompt is leak-scanned.
  5. The tripwire is proven to fail closed: a poisoned raw pull makes the sanitizer
     raise and write nothing.
  6. The filesystem-path screen is proven precise: it ignores URLs, dates, and
     ratios, and still catches genuine absolute paths.

On success it prints ALL CHECKS PASSED and "Safe to film" and exits 0. Any leak or
mismatch prints FAIL and exits 1.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from sanitize import (
    RAW_PATH,
    SanitizationError,
    assert_pseudonyms,
    build_snapshot,
    sanitize,
    scan_forbidden,
)
from mcp_server import SNAPSHOT, SYSTEM_PROMPT, TOOLS

HERE = Path(__file__).resolve().parent
EXPECTED_PATH = HERE / "fixtures" / "expected_snapshot.json"

PASS = "PASS"
FAIL = "FAIL"
_failures: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = PASS if ok else FAIL
    line = f"  [{mark}] {label}"
    if detail:
        line += f" :: {detail}"
    print(line)
    if not ok:
        _failures.append(label)


def main() -> int:
    print("Watchtower frame check")
    print("-" * 52)

    # 1. Loaded snapshot is clean and fully pseudonymized.
    hits = scan_forbidden(json.dumps(SNAPSHOT, ensure_ascii=False))
    _check("loaded snapshot passes forbidden-token screen", not hits, "; ".join(hits))
    try:
        assert_pseudonyms(SNAPSHOT)
        _check("every component label is a pseudonym", True)
    except SanitizationError as exc:
        _check("every component label is a pseudonym", False, str(exc))

    # 2. Transform faithfulness: sanitize(raw) == shipped expected snapshot.
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    produced = sanitize(raw)
    _check("sanitize(raw_sample) reproduces expected_snapshot", produced == expected)

    # 3. Every tool output is leak-free.
    for name, fn in TOOLS.items():
        try:
            out = fn()
            out_hits = scan_forbidden(json.dumps(out, ensure_ascii=False))
            _check(f"tool {name} output is clean", not out_hits, "; ".join(out_hits))
        except SanitizationError as exc:
            _check(f"tool {name} output is clean", False, str(exc))

    # 4. System prompt carries no private token.
    sp_hits = scan_forbidden(SYSTEM_PROMPT)
    _check("system prompt is clean", not sp_hits, "; ".join(sp_hits))

    # 5. Tripwire fails closed on a poisoned raw pull.
    poisoned = copy.deepcopy(raw)
    poisoned["alerts"]["events"][0]["alert_class"] = "leak /var/secrets/private.key"
    tripped = False
    try:
        build_snapshot(poisoned)
        # build_snapshot alone does not screen; sanitize() must reject.
        sanitize(poisoned)
    except SanitizationError:
        tripped = True
    _check("poisoned raw pull is rejected (fail-closed)", tripped)

    # 6. The path screen is precise: benign slashes pass, absolute paths trip.
    benign = ("badge at https://img.shields.io/badge/python-3.11, "
              "released 2026/07/14, uptime 24/7, see fixtures/raw_sample.json")
    benign_hits = scan_forbidden(benign)
    _check("path screen ignores URLs, dates, and ratios", not benign_hits,
           "; ".join(benign_hits))
    leaky = "component log at /var/log/fleet/engine-1.log"
    leak_hits = scan_forbidden(leaky)
    _check("path screen still catches absolute paths",
           any(h.startswith("filesystem-path") for h in leak_hits))

    print("-" * 52)
    if _failures:
        print(f"{FAIL}: {len(_failures)} check(s) failed. NOT safe to film.")
        return 1
    print("ALL CHECKS PASSED")
    print("Safe to film.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
