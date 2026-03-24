#!/usr/bin/env python3
"""
agent.py - Run a GitHub Copilot CLI coding agent in a loop

Reads a task prompt from a Markdown file and repeatedly invokes the Copilot CLI
to execute it.  The agent logs every prompt and response, tracks git commits,
and loops until the user presses Ctrl-C (or a maximum iteration count is hit).

Usage:
    python agent.py --prompt prompt.md
    python agent.py --prompt prompt.md --max-iterations 10
    python agent.py --prompt prompt.md --dir ../MyProject --dir ../Shared
    python agent.py --prompt prompt.md --delay 15 --model claude-opus-4.6
    python agent.py --prompt prompt.md --once

Prerequisites:
    - GitHub CLI installed with Copilot extension (`gh copilot` on PATH)
"""

import subprocess
import sys
import os
import time
import shutil
import argparse
import select
import threading
from pathlib import Path
from datetime import datetime

from setup import run_setup

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-opus-4.6"
DEFAULT_DELAY = 30  # seconds between iterations
DEFAULT_IDLE_TIMEOUT = 300  # kill if no output for 5 minutes
DEFAULT_ITERATION_TIMEOUT = 3600  # kill after 60 minutes total per iteration

# Force UTF-8 on Windows
if sys.platform == "win32":
    os.system("chcp 65001 > nul 2>&1")
    os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_file = None


def init_log(log_dir: Path) -> Path:
    """Open a timestamped log file and return its path."""
    global _log_file
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"agent_{stamp}.log"
    _log_file = open(log_path, "w", encoding="utf-8")
    log(f"Log started at {datetime.now().isoformat()}")
    return log_path


def close_log():
    global _log_file
    if _log_file:
        log(f"Log ended at {datetime.now().isoformat()}")
        _log_file.close()
        _log_file = None


def log(message: str):
    global _log_file
    if _log_file:
        _log_file.write(message + "\n")
        _log_file.flush()


def log_section(label: str, content: str):
    log(f"\n{'=' * 60}")
    log(f"{label}  [{datetime.now().isoformat()}]")
    log(f"{'=' * 60}")
    log(content)
    log(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_head(cwd: Path) -> str:
    """Return the current HEAD commit hash, or '' on failure."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_has_uncommitted(cwd: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Prompt handling
# ---------------------------------------------------------------------------

def load_prompt(path: Path) -> str:
    """Read the user prompt file."""
    if not path.exists():
        print(f"ERROR: Prompt file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def build_full_prompt(user_prompt: str, iteration: int) -> str:
    """Wrap the user prompt with housekeeping instructions."""
    return f"""## Iteration {iteration}

{user_prompt}

---

## Housekeeping (always follow these)

1. **Commit regularly.** After every meaningful change, stage and commit with a
   clear message describing what you did.  Small, frequent commits are better
   than one large commit at the end.
2. **Log your progress.** Before starting work, briefly state your plan.  After
   completing a step, summarize what was done and what remains.
3. **Stay on task.** Focus only on the instructions above.  Do not refactor
   unrelated code or add features that were not requested.
4. **Stop when done.** If there is nothing left to do, say "TASK COMPLETE" and
   stop.
"""


# ---------------------------------------------------------------------------
# Copilot invocation
# ---------------------------------------------------------------------------

def run_copilot(prompt: str, *, copilot_cli: str, model: str,
                work_dir: Path, extra_dirs: list[Path],
                iteration: int,
                idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
                iteration_timeout: int = DEFAULT_ITERATION_TIMEOUT) -> int:
    """Invoke the Copilot CLI with the given prompt.

    Returns the process exit code.

    Protections against hanging:
      - stdin is /dev/null so the agent can never block on user input
      - idle_timeout kills the process if no output for N seconds
      - iteration_timeout kills the process after N seconds total
    """
    # Write the prompt to a temp file to avoid command-line length limits
    prompt_dir = work_dir / "logs"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompt_dir / f"prompt_iter{iteration}.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    short_prompt = (
        f"Read and follow ALL instructions in the file: {prompt_file}\n"
        f"That file contains your complete task description."
    )

    cmd = [
        copilot_cli, "copilot",
        "--",
        "--model", model,
        "--allow-all-tools",
        "--allow-all-paths",
        "-p", short_prompt,
        "--add-dir", str(work_dir),
    ]
    for d in extra_dirs:
        if d.exists():
            cmd.extend(["--add-dir", str(d)])

    log_section(f"PROMPT (iteration {iteration})", prompt)

    print()
    print("=" * 60)
    print(f"  Iteration {iteration}")
    print("=" * 60)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        output_lines = []
        iteration_start = time.monotonic()
        last_output_time = time.monotonic()
        timed_out = False
        timeout_reason = ""

        def _read_output():
            """Read stdout in a background thread to allow timeout checks."""
            nonlocal last_output_time
            try:
                for line in proc.stdout:
                    output_lines.append(line)
                    last_output_time = time.monotonic()
                    print(line, end="")
                    log(line.rstrip())
            except ValueError:
                pass  # stdout closed

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        try:
            while reader.is_alive():
                reader.join(timeout=5)
                now = time.monotonic()

                # Check idle timeout
                if idle_timeout > 0 and (now - last_output_time) > idle_timeout:
                    timed_out = True
                    timeout_reason = (
                        f"No output for {idle_timeout}s (idle timeout)"
                    )
                    break

                # Check iteration timeout
                if iteration_timeout > 0 and (now - iteration_start) > iteration_timeout:
                    timed_out = True
                    timeout_reason = (
                        f"Iteration exceeded {iteration_timeout}s (iteration timeout)"
                    )
                    break

        except KeyboardInterrupt:
            print("\n\nTerminating Copilot subprocess...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise

        if timed_out:
            msg = f"TIMEOUT: {timeout_reason} — killing iteration {iteration}"
            print(f"\n{msg}")
            log(msg)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            reader.join(timeout=5)
        else:
            proc.wait()

        output = "".join(output_lines)
        log_section(f"OUTPUT (iteration {iteration})", output)

        if timed_out:
            return 1

        if proc.returncode != 0:
            msg = f"Copilot CLI exited with code {proc.returncode}"
            print(f"\n{msg}")
            log(msg)

        return proc.returncode

    except FileNotFoundError:
        print("ERROR: 'gh' CLI not found. Is it installed and on PATH?",
              file=sys.stderr)
        log("ERROR: gh CLI not found")
        return 1
    except Exception as e:
        print(f"ERROR running Copilot: {e}", file=sys.stderr)
        log(f"ERROR: {e}")
        return 1


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run a Copilot coding agent in a loop",
    )
    parser.add_argument(
        "--prompt", required=True, type=str, metavar="FILE",
        help="Path to the Markdown file containing the task prompt",
    )
    parser.add_argument(
        "--dir", action="append", default=[], metavar="PATH",
        help="Additional directory to expose to the agent (repeatable). "
             "The first --dir is also used as the working directory. "
             "If omitted the current directory is used.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Copilot model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--delay", type=int, default=DEFAULT_DELAY, metavar="SECONDS",
        help=f"Seconds to wait between iterations (default: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=0, metavar="N",
        help="Stop after N iterations (0 = unlimited, default: 0)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one iteration then exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the full prompt and exit without running Copilot",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT, metavar="SECONDS",
        help=f"Kill if no output for this many seconds (default: {DEFAULT_IDLE_TIMEOUT}, 0=disabled)",
    )
    parser.add_argument(
        "--iteration-timeout", type=int, default=DEFAULT_ITERATION_TIMEOUT, metavar="SECONDS",
        help=f"Max seconds per iteration (default: {DEFAULT_ITERATION_TIMEOUT}, 0=disabled)",
    )

    args = parser.parse_args()

    # Load config, set env vars, install/verify dependencies
    run_setup()

    # Resolve dirs
    work_dir = Path(args.dir[0]).resolve() if args.dir else Path.cwd().resolve()
    extra_dirs = [Path(d).resolve() for d in args.dir[1:]]

    prompt_path = Path(args.prompt).resolve()
    user_prompt = load_prompt(prompt_path)

    if args.once:
        args.max_iterations = 1

    # Dry run
    if args.dry_run:
        full = build_full_prompt(user_prompt, iteration=1)
        print(full)
        sys.exit(0)

    # Verify gh CLI
    copilot_cli = shutil.which("gh")
    if not copilot_cli:
        print("WARNING: 'gh' not found on PATH; will try anyway.")
        copilot_cli = "gh"

    # Init logging
    log_dir = work_dir / "logs"
    log_path = init_log(log_dir)

    print(f"Prompt:        {prompt_path}")
    print(f"Working dir:   {work_dir}")
    print(f"Extra dirs:    {extra_dirs or '(none)'}")
    print(f"Model:         {args.model}")
    print(f"Delay:         {args.delay}s")
    print(f"Idle timeout:  {args.idle_timeout}s")
    print(f"Iter timeout:  {args.iteration_timeout}s")
    print(f"Max iters:     {args.max_iterations or 'unlimited'}")
    print(f"Log file:      {log_path}")
    print()

    log(f"Prompt file: {prompt_path}")
    log(f"Working dir: {work_dir}")
    log(f"Model: {args.model}")
    log(f"Max iterations: {args.max_iterations or 'unlimited'}")

    iteration = 0
    try:
        while True:
            iteration += 1

            if args.max_iterations and iteration > args.max_iterations:
                break

            # Re-read prompt each iteration so the user can edit it live
            user_prompt = load_prompt(prompt_path)
            full_prompt = build_full_prompt(user_prompt, iteration)

            commit_before = git_head(work_dir)

            run_copilot(
                full_prompt,
                copilot_cli=copilot_cli,
                model=args.model,
                work_dir=work_dir,
                extra_dirs=extra_dirs,
                iteration=iteration,
                idle_timeout=args.idle_timeout,
                iteration_timeout=args.iteration_timeout,
            )

            # Report commit status
            commit_after = git_head(work_dir)
            if commit_before and commit_after:
                if commit_before != commit_after:
                    msg = f"Agent committed (HEAD now {commit_after[:8]})"
                    print(f"\n>> {msg}")
                    log(msg)
                elif git_has_uncommitted(work_dir):
                    msg = "WARNING: Agent did NOT commit. Uncommitted changes detected."
                    print(f"\n>> {msg}")
                    log(msg)
                else:
                    log("No commit and no uncommitted changes this iteration.")

            # Delay before next iteration
            is_last = args.max_iterations and iteration >= args.max_iterations
            if not is_last and args.delay > 0:
                print(f"\nWaiting {args.delay}s before next iteration...")
                log(f"Waiting {args.delay}s...")
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\nStopped by user (Ctrl-C).")
        log("Stopped by user.")
    finally:
        print()
        print("=" * 60)
        print(f"Session complete.  {iteration} iteration(s) run.")
        print(f"Log: {log_path}")
        print("=" * 60)
        close_log()


if __name__ == "__main__":
    main()
