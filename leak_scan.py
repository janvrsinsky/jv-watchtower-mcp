"""Repo-wide structural leak scan. Runs offline on the standard library alone.

The pipeline gate (test_flow.py) proves the tool surface is clean. This scan
gives the CI badge a second, wider subject: every tracked file in the repository
is screened for the shapes of real-world leaks, so a green badge attests to the
whole public tree, and the pipeline gate covers the synthetic data flow.

What it screens for, structurally (no private vocabulary ships in this repo):

  * credential assignments (key, secret, token, or password variables set to
    long quoted literals)
  * private key blocks (PEM-style BEGIN markers)
  * vendor token formats (cloud access key ids, repo-host tokens, chat tokens,
    secret-key style bearer tokens)
  * private network addresses (RFC 1918 and loopback IPv4)
  * machine-specific absolute paths (user home directories on macOS, Linux,
    and Windows)
  * personal identifiers (email addresses)

Two files are excluded from the tree walk, each for a documented reason:

  * fixtures/raw_sample.json is the deliberately dirty synthetic input. Its job
    is to carry forbidden shapes (fake internal addresses, raw component ids) so
    the pipeline can prove the sanitizer strips them; test_flow.py asserts that
    the snapshot built from it is clean.
  * fixtures/leak_scan_negative_control.txt is the planted-secret canary. It is
    scanned separately below, and the run FAILS unless every planted pattern is
    caught. A green run therefore also proves the scanner is able to fail.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

DIRTY_FIXTURE = "fixtures/raw_sample.json"
NEGATIVE_CONTROL = "fixtures/leak_scan_negative_control.txt"
EXCLUDED = {DIRTY_FIXTURE, NEGATIVE_CONTROL}

# Structural patterns only. Every pattern must be caught in the negative
# control file, so adding a pattern here without planting a matching fake
# value there fails the run.
TREE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("credential-assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b"
                r"\s*[:=]\s*['\"][A-Za-z0-9+/_\-]{12,}['\"]")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("cloud-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("repo-host-token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("chat-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("secret-key-token", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private-network-address",
     re.compile(r"\b(?:10|127)(?:\.\d{1,3}){3}\b"
                r"|\b(?:169\.254|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))(?:\.\d{1,3}){2}\b")),
    ("machine-path",
     re.compile(r"(?<![\w:./-])/(?:Users|home|Volumes)/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*")),
    ("windows-user-path", re.compile(r"\b[A-Za-z]:\\Users\\[^\s\"']+")),
    ("email-address",
     re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
]


def tracked_files() -> list[str]:
    """Every file git tracks; falls back to a directory walk without git."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=HERE, check=True, capture_output=True, text=True,
        ).stdout
        return [name for name in out.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        skip_parts = {".git", "__pycache__", "snapshot", ".venv", "venv"}
        return [
            str(p.relative_to(HERE))
            for p in sorted(HERE.rglob("*"))
            if p.is_file() and not skip_parts.intersection(p.parts)
        ]


def scan_lines(text: str) -> list[tuple[str, int, str]]:
    """Return (pattern_name, line_number, matched_text) for every hit."""
    hits: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in TREE_PATTERNS:
            for match in pattern.findall(line):
                hits.append((name, lineno, match))
    return hits


def _read_text(path: Path) -> str | None:
    """File contents, or None for binary files (null byte in the first 8 KiB)."""
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def main() -> int:
    print("Watchtower repo-wide leak scan")
    print("-" * 52)
    failures = 0

    # 1. Every tracked file must be clean.
    scanned = 0
    for name in tracked_files():
        if name in EXCLUDED:
            continue
        text = _read_text(HERE / name)
        if text is None:  # binary, e.g. assets
            continue
        scanned += 1
        for pattern_name, lineno, match in scan_lines(text):
            print(f"  [FAIL] {name}:{lineno} {pattern_name}: {match!r}")
            failures += 1
    print(f"  [{'FAIL' if failures else 'PASS'}] tracked tree is clean"
          f" ({scanned} text files scanned, {len(EXCLUDED)} documented exclusions)")

    # 2. Negative control: every pattern must catch its planted fake value.
    control_path = HERE / NEGATIVE_CONTROL
    if not control_path.exists():
        print(f"  [FAIL] negative control missing: {NEGATIVE_CONTROL}")
        failures += 1
    else:
        caught = {name for name, _, _ in scan_lines(_read_text(control_path) or "")}
        missed = [name for name, _ in TREE_PATTERNS if name not in caught]
        for name in missed:
            print(f"  [FAIL] pattern {name} missed its planted value in the negative control")
        failures += len(missed)
        print(f"  [{'FAIL' if missed else 'PASS'}] negative control: "
              f"{len(caught)}/{len(TREE_PATTERNS)} planted patterns caught")

    print("-" * 52)
    if failures:
        print(f"FAIL: {failures} problem(s).")
        return 1
    print("ALL CLEAN: tree passes, and the scanner proved it can fail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
