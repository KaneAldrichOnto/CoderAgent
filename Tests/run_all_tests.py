#!/usr/bin/env python3
"""
run_all_tests.py - Run every Tests/test_*.md prompt through agent.py.

Each test prompt is run in its own scratch working directory under
Tests/_runs/<test_name>/<timestamp>/, so artifacts, commits, scratchpads,
and logs from one test don't bleed into another. The repo root is
exposed as an extra --dir so the agent can still see doc_tools.py and
gui_nav.py.

Success criterion per test: agent.py exits 0 AND a `.agent_done` file
exists in the scratch dir at the end.

Usage
-----

    python Tests/run_all_tests.py                  # run all tests
    python Tests/run_all_tests.py --filter image   # only test_doc_tools_image
    python Tests/run_all_tests.py --once           # forward --once to agent.py
    python Tests/run_all_tests.py --keep-runs 3    # keep last 3 run dirs each
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
RUNS_DIR = TESTS_DIR / "_runs"
DONE_FILE = ".agent_done"
# agent.py prints this exact line to stdout when it sees `.agent_done`
# in the working directory. We detect that, because agent.py *deletes*
# the sentinel as soon as it consumes it, so the file is gone by the
# time this runner gets to look at the directory.
DONE_MARKER = "Agent signalled TASK COMPLETE"


# ---------------------------------------------------------------------------
# Self-elevation
# ---------------------------------------------------------------------------
# agent.py self-elevates on Windows by spawning a NEW admin console via
# ShellExecuteW("runas") and exiting the original process with code 0
# immediately. That makes the runner see "exit 0, no .agent_done" and
# report every test as a spurious failure, while the real work continues
# in detached admin windows we have no handle on.
#
# Fix: elevate the runner ONCE at startup (same UAC mechanic), then run
# every agent.py invocation synchronously inside that elevated process
# with --no-elevate so it does not fork off again. Admin permissions
# propagate to children, so gui_nav still works against admin-integrity
# targets, AND we can actually wait on each test.
# ---------------------------------------------------------------------------

def _is_elevated_windows() -> bool:
    if sys.platform != "win32":
        return True  # treat non-Windows as "no elevation needed"
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _self_elevate_runner(argv: list[str]) -> None:
    """Re-launch *this script* elevated and exit. Windows-only."""
    if sys.platform != "win32":
        return
    if _is_elevated_windows():
        return
    if "--no-elevate" in argv:
        return

    script = os.path.abspath(__file__)

    def _q(s: str) -> str:
        if not s or any(c in s for c in ' \t"'):
            return '"' + s.replace('"', '\\"') + '"'
        return s

    forwarded = [a for a in argv if a != "--no-elevate"]
    inner = " ".join(_q(a) for a in [sys.executable, script, *forwarded, "--no-elevate"])
    # /k keeps the admin window open so the user can read the summary.
    cmd_line = f'/k cd /d {_q(os.getcwd())} && {inner}'

    print("[elevate] Not running as admin; relaunching the test runner in "
          "an elevated console via UAC. Pass --no-elevate to skip.",
          flush=True)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_line, None, 1)
    if rc <= 32:
        print(f"[elevate] UAC elevation failed (rc={rc}). Re-run from an "
              "already-elevated PowerShell, or pass --no-elevate to run "
              "without elevation (some GUI tests will hit permission errors).",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def discover_tests(filter_substr: str = "") -> list[Path]:
    tests = sorted(TESTS_DIR.glob("test_*.md"))
    if filter_substr:
        tests = [t for t in tests if filter_substr.lower() in t.stem.lower()]
    return tests


def prune_runs(test_runs_dir: Path, keep: int) -> None:
    if keep <= 0 or not test_runs_dir.exists():
        return
    runs = sorted(
        (p for p in test_runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def run_one(test: Path, agent_args: list[str], keep_runs: int) -> tuple[bool, Path, float]:
    """Run a single test prompt. Returns (passed, run_dir, elapsed_seconds)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_runs_dir = RUNS_DIR / test.stem
    run_dir = test_runs_dir / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # Copy the prompt into the scratch dir as prompt.md (agent.py reads
    # whatever --prompt points at; relative paths inside the prompt are
    # resolved against the agent's cwd, which is the scratch dir).
    prompt_copy = run_dir / "prompt.md"
    shutil.copyfile(test, prompt_copy)

    cmd = [
        sys.executable,
        str(REPO_ROOT / "agent.py"),
        "--prompt", str(prompt_copy),
        "--dir", str(run_dir),       # working directory
        "--dir", str(REPO_ROOT),     # so the agent can see doc_tools/gui_nav
        # The runner self-elevates once at startup (see _self_elevate_runner);
        # admin propagates to children, so each agent.py call must NOT
        # fork off into yet another detached UAC window.
        "--no-elevate",
        *agent_args,
    ]

    start = time.monotonic()
    print(f"\n=== RUN: {test.name}")
    print(f"    scratch: {run_dir}")
    print(f"    cmd:     {' '.join(cmd)}\n", flush=True)

    # Stream agent.py output to our stdout while also scanning each line
    # for the DONE_MARKER. We can't rely on a `.agent_done` file because
    # agent.py removes the sentinel as soon as it consumes it.
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    saw_done_marker = False
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if DONE_MARKER in line:
            saw_done_marker = True
    rc = proc.wait()
    elapsed = time.monotonic() - start

    # `.agent_done` is normally already deleted by agent.py — keep the
    # check as a fallback in case it failed mid-cleanup.
    done = saw_done_marker or (run_dir / DONE_FILE).exists()
    passed = rc == 0 and done

    prune_runs(test_runs_dir, keep_runs)
    return passed, run_dir, elapsed


def main() -> int:
    # Elevate this process FIRST, before doing anything else, so all
    # subsequent agent.py calls inherit admin permissions.
    _self_elevate_runner(sys.argv[1:])

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--filter", default="", metavar="SUBSTR",
                   help="Only run tests whose filename contains SUBSTR")
    p.add_argument("--keep-runs", type=int, default=5, metavar="N",
                   help="Keep only the last N run dirs per test (default: 5, 0=keep all)")
    p.add_argument("--stop-on-fail", action="store_true",
                   help="Abort the suite on the first failure")
    p.add_argument("--no-elevate", action="store_true",
                   help="Skip the runner's own UAC self-elevation. Some "
                        "GUI tests will hit permission errors without admin.")
    # Everything after `--` is forwarded verbatim to agent.py.
    p.add_argument("agent_args", nargs=argparse.REMAINDER,
                   help="Args after `--` are forwarded to agent.py "
                        "(e.g. -- --once --max-iterations 5)")
    args = p.parse_args()

    forwarded = args.agent_args
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    tests = discover_tests(args.filter)
    if not tests:
        print(f"No tests found in {TESTS_DIR} matching filter={args.filter!r}", file=sys.stderr)
        return 1

    print(f"Discovered {len(tests)} test(s):")
    for t in tests:
        print(f"  - {t.name}")

    results: list[tuple[Path, bool, Path, float]] = []
    for test in tests:
        passed, run_dir, elapsed = run_one(test, forwarded, args.keep_runs)
        results.append((test, passed, run_dir, elapsed))
        status = "PASS" if passed else "FAIL"
        print(f"\n--- {status}: {test.name} ({elapsed:.1f}s)")
        if not passed and args.stop_on_fail:
            print("Stopping suite (--stop-on-fail).")
            break

    print("\n========== Summary ==========")
    failed = 0
    for test, passed, run_dir, elapsed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {test.name:40s}  {elapsed:6.1f}s  {run_dir}")
        if not passed:
            failed += 1
    print(f"=============================")
    print(f"{len(results) - failed}/{len(results)} passed")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
