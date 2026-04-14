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
DONE_SIGNAL_FILE = ".agent_done"  # agent creates this file to end early

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


def git_new_commits(cwd: Path, old_sha: str, new_sha: str) -> list[tuple[str, str]]:
    """Return a list of (hash, message) for commits between old_sha and new_sha."""
    try:
        if old_sha:
            cmd = ["git", "log", "--format=%H%n%B%n---END---", f"{old_sha}..{new_sha}"]
        else:
            # No previous HEAD (first commit(s) in the repo)
            cmd = ["git", "log", "--format=%H%n%B%n---END---", new_sha]
        r = subprocess.run(
            cmd,
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        commits = []
        entries = r.stdout.split("---END---")
        for entry in entries:
            lines = entry.strip().splitlines()
            if not lines:
                continue
            sha = lines[0].strip()
            message = "\n".join(lines[1:]).strip()
            if sha:
                commits.append((sha, message))
        return commits
    except Exception:
        return []


def git_has_uncommitted(cwd: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def git_diff_stat(cwd: Path, sha: str) -> str:
    """Return the --stat output for a single commit."""
    try:
        r = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--stat", "-r", sha],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_changed_files(cwd: Path, sha: str) -> list[str]:
    """Return list of files changed in a single commit."""
    try:
        r = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return [f for f in r.stdout.strip().splitlines() if f]
        return []
    except Exception:
        return []


def git_diff_patch(cwd: Path, sha: str, max_bytes: int = 8000) -> str:
    """Return the unified diff (patch) for a single commit, truncated if large."""
    try:
        r = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "-p", "-r", sha],
            cwd=str(cwd), capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return ""
        patch = r.stdout.strip()
        if len(patch) > max_bytes:
            patch = patch[:max_bytes] + "\n... (diff truncated)"
        return patch
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Internal commit logging
# ---------------------------------------------------------------------------
AGENT_DIR = Path(__file__).resolve().parent
INTERNAL_LOGS_DIR = AGENT_DIR / "InternalLogs"


def init_internal_logs():
    """Create the InternalLogs directory if it doesn't exist."""
    INTERNAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_commits(cwd: Path, old_sha: str, new_sha: str):
    """Write a timestamped log file for each new commit."""
    commits = git_new_commits(cwd, old_sha, new_sha)
    for sha, message in commits:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{stamp}_{sha[:8]}.log"
        log_path = INTERNAL_LOGS_DIR / filename
        log_path.write_text(
            f"Timestamp: {datetime.now().isoformat()}\n"
            f"Commit:    {sha}\n\n"
            f"{message}\n",
            encoding="utf-8",
        )
        log(f"Commit log written: {log_path}")


COMMIT_LOG_FILE = "commit_log.md"


def append_commit_log(cwd: Path, old_sha: str, new_sha: str):
    """Append human-readable entries to commit_log.md in the working directory."""
    commits = git_new_commits(cwd, old_sha, new_sha)
    if not commits:
        return

    log_path = cwd / COMMIT_LOG_FILE

    # Create the file with a header if it doesn't exist yet
    if not log_path.exists():
        log_path.write_text(
            "# Commit Log\n\n"
            "Auto-generated by CoderAgent. Each entry describes a commit: "
            "what changed, why, and which files were affected.\n\n",
            encoding="utf-8",
        )

    entries = []
    for sha, message in commits:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed_files = git_changed_files(cwd, sha)
        diff_stat = git_diff_stat(cwd, sha)
        patch = git_diff_patch(cwd, sha)

        # Split commit message into subject and body
        msg_lines = message.strip().splitlines()
        subject = msg_lines[0] if msg_lines else "(no message)"
        body = "\n".join(msg_lines[1:]).strip() if len(msg_lines) > 1 else ""

        entry = f"---\n\n"
        entry += f"## `{sha[:8]}` — {subject}\n\n"
        entry += f"**Date:** {stamp}\n\n"

        if body:
            entry += f"**Details:**\n\n{body}\n\n"

        if changed_files:
            entry += f"**Files changed ({len(changed_files)}):**\n\n"
            for f in changed_files:
                entry += f"- `{f}`\n"
            entry += "\n"

        if diff_stat:
            entry += f"**Diff summary:**\n\n```\n{diff_stat}\n```\n\n"

        if patch:
            entry += f"**Changes:**\n\n```diff\n{patch}\n```\n\n"

        entries.append(entry)

    with open(log_path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry)

    log(f"Commit log updated: {log_path} ({len(entries)} new entries)")


# ---------------------------------------------------------------------------
# Prompt handling
# ---------------------------------------------------------------------------

def load_prompt(path: Path) -> str:
    """Read the user prompt file, creating it from the example template if needed."""
    if not path.exists():
        example = path.parent / "prompt.example.md"
        if example.exists():
            shutil.copy2(example, path)
            print(f"Created {path.name} from example template.")
            print(f"Please edit {path} with your task, then re-run the agent.")
            sys.exit(1)
        print(f"ERROR: Prompt file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def load_scratchpad(work_dir: Path) -> str:
    """Read the agent scratchpad file, or return '' if it doesn't exist yet."""
    pad = work_dir / "agent_scratchpad.md"
    if pad.exists():
        return pad.read_text(encoding="utf-8")
    return ""


def load_steering(work_dir: Path) -> str:
    """Read operator override instructions from steering.md, if present."""
    steer = work_dir / "steering.md"
    if steer.exists():
        content = steer.read_text(encoding="utf-8").strip()
        if content:
            return content
    return ""


def check_done_signal(work_dir: Path) -> bool:
    """Check if the agent signalled task completion; remove the file if found."""
    signal = work_dir / DONE_SIGNAL_FILE
    if signal.exists():
        signal.unlink()
        return True
    return False


def build_full_prompt(user_prompt: str, iteration: int,
                      scratchpad: str, last_commits: str,
                      steering: str, test_results: str) -> str:
    """Wrap the user prompt with scratchpad contents and housekeeping."""
    if steering.strip():
        steering_section = (
            "## [OPERATOR OVERRIDE — read and follow this before anything else]\n\n"
            f"{steering}\n\n"
            "---\n\n"
        )
    else:
        steering_section = ""

    if last_commits.strip():
        commit_history_section = (
            "\n---\n\n"
            "## Commits you made last iteration (ground truth)\n\n"
            f"{last_commits}\n"
        )
    else:
        commit_history_section = (
            "\n---\n\n"
            "## Commits you made last iteration\n\n"
            "*(none — either this is the first iteration or no commits were made)*\n"
        )

    if test_results.strip():
        test_section = (
            "\n---\n\n"
            "## Test Results from Last Iteration\n\n"
            f"{test_results}\n"
        )
    else:
        test_section = ""

    scratchpad_section = ""
    if scratchpad.strip():
        scratchpad_section = (
            "\n---\n\n"
            "## Scratchpad (notes from your previous iteration)\n\n"
            f"{scratchpad}\n"
        )
    else:
        scratchpad_section = (
            "\n---\n\n"
            "## Scratchpad\n\n"
            "*(empty — this is the first iteration, or no notes were saved)*\n"
        )

    return f"""{steering_section}## Iteration {iteration}

{user_prompt}
{commit_history_section}
{test_section}
{scratchpad_section}
---

## Housekeeping (always follow these)

1. **Commit regularly.** After every meaningful change, stage and commit with a
   clear message describing what you did.  Small, frequent commits are better
   than one large commit at the end.
2. **Log your progress.** Before starting work, briefly state your plan.  After
   completing a step, summarize what was done and what remains.
3. **Stay on task.** Focus only on the instructions above.  Do not refactor
   unrelated code or add features that were not requested.
4. **Stop when done.** When the task is fully complete and there is nothing left
   to do, create an empty file called `.agent_done` in the working directory
   (e.g. run `touch .agent_done` or write an empty file).  This signals the
   outer loop to stop iterating.  You should still commit your work first.
5. **Update the scratchpad.** Before stopping, write notes to
   `agent_scratchpad.md` in the working directory.  Include:
   - What you accomplished this iteration
   - What still needs to be done
   - Any problems, blockers, or decisions for the next iteration
   - Key file paths or context the next iteration will need
   Do NOT commit this file — it is git-ignored.
"""


# ---------------------------------------------------------------------------
# Copilot invocation
# ---------------------------------------------------------------------------

def run_test_cmd(cmd: str, work_dir: Path) -> tuple[int, str]:
    """Run the test command and return (exit_code, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,  # 5-minute hard cap for test runs
        )
        output = (result.stdout + result.stderr).strip()
        # Truncate very long output to avoid overwhelming the prompt
        max_chars = 6000
        if len(output) > max_chars:
            output = output[:max_chars] + "\n... (output truncated)"
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 1, "ERROR: Test command timed out after 300 seconds."
    except Exception as e:
        return 1, f"ERROR running test command: {e}"


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


def run_claude(prompt: str, *, claude_cli: str, model: str,
               work_dir: Path, extra_dirs: list[Path],
               iteration: int,
               idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
               iteration_timeout: int = DEFAULT_ITERATION_TIMEOUT) -> int:
    """Invoke the Claude Code CLI in headless mode with the given prompt.

    Returns the process exit code.

    Claude Code headless flags (discovered via docs):
      - -p / --print: headless mode, accepts prompt string
      - --model: model selection
      - --allowedTools: tool permissions (use Bash,Read,Write,Edit etc.)
      - --output-format: text|json|stream-json
      - No --add-dir equivalent; cwd controls working directory
    """
    # Write prompt to file (same pattern as run_copilot)
    prompt_dir = work_dir / "logs"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompt_dir / f"prompt_iter{iteration}.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Claude Code reads the prompt directly via -p flag
    full_inline_prompt = (
        f"Read and follow ALL instructions in the file: {prompt_file}\n"
        f"That file contains your complete task description for this iteration."
    )

    cmd = [claude_cli, "--print", "--model", model, "-p", full_inline_prompt,
           "--output-format", "stream-json"]

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
            print("\n\nTerminating Claude subprocess...")
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
            msg = f"Claude CLI exited with code {proc.returncode}"
            print(f"\n{msg}")
            log(msg)

        return proc.returncode

    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found. Is it installed and on PATH?",
              file=sys.stderr)
        log("ERROR: claude CLI not found")
        return 1
    except Exception as e:
        print(f"ERROR running Claude: {e}", file=sys.stderr)
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
    parser.add_argument(
        "--test-cmd", default="", metavar="CMD",
        help="Shell command to run after each iteration (e.g. 'pytest tests/'). "
             "Output is injected into the next iteration's prompt. "
             "Also used to validate the .agent_done signal before stopping.",
    )
    parser.add_argument(
        "--backend", choices=["copilot", "claude"], default="claude",
        help="Which CLI backend to use: 'claude' (default) or 'copilot' (legacy gh copilot)",
    )

    args = parser.parse_args()

    # Load config, set env vars, install/verify dependencies
    run_setup(backend=args.backend)

    # Resolve dirs
    work_dir = Path(args.dir[0]).resolve() if args.dir else Path.cwd().resolve()
    extra_dirs = [Path(d).resolve() for d in args.dir[1:]]

    prompt_path = Path(args.prompt).resolve()
    user_prompt = load_prompt(prompt_path)

    if args.once:
        args.max_iterations = 1

    # Dry run
    if args.dry_run:
        scratchpad = load_scratchpad(work_dir)
        full = build_full_prompt(user_prompt, iteration=1, scratchpad=scratchpad,
                                last_commits="", steering="", test_results="")
        print(full)
        sys.exit(0)

    # Resolve CLI backend
    if args.backend == "claude":
        agent_cli = shutil.which("claude") or "claude"
        backend_run = lambda prompt, **kw: run_claude(prompt, claude_cli=agent_cli, **kw)
    else:
        agent_cli = shutil.which("gh") or "gh"
        backend_run = lambda prompt, **kw: run_copilot(prompt, copilot_cli=agent_cli, **kw)

    # Init logging
    log_dir = work_dir / "logs"
    log_path = init_log(log_dir)
    init_internal_logs()

    print(f"Backend:       {args.backend}")
    print(f"Prompt:        {prompt_path}")
    print(f"Working dir:   {work_dir}")
    print(f"Extra dirs:    {extra_dirs or '(none)'}")
    print(f"Model:         {args.model}")
    print(f"Delay:         {args.delay}s")
    print(f"Idle timeout:  {args.idle_timeout}s")
    print(f"Iter timeout:  {args.iteration_timeout}s")
    print(f"Max iters:     {args.max_iterations or 'unlimited'}")
    print(f"Test command:  {args.test_cmd or '(none)'}")
    print(f"Log file:      {log_path}")
    print(f"Rollback tag:  (created after setup)")
    print()

    log(f"Prompt file: {prompt_path}")
    log(f"Working dir: {work_dir}")
    log(f"Model: {args.model}")
    log(f"Max iterations: {args.max_iterations or 'unlimited'}")

    # Clear any stale done signal from a previous run
    stale_signal = work_dir / DONE_SIGNAL_FILE
    if stale_signal.exists():
        stale_signal.unlink()
        log("Cleared stale .agent_done signal from previous run.")

    # Create a rollback tag so the user can restore to pre-run state via:
    #   git reset --hard agent-run-<timestamp>
    _rollback_tag = f"agent-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    tag_result = subprocess.run(
        ["git", "tag", _rollback_tag],
        cwd=str(work_dir), capture_output=True, text=True, timeout=10,
    )
    if tag_result.returncode == 0:
        print(f"Rollback tag created: {_rollback_tag}")
        log(f"Rollback tag: {_rollback_tag}")
    else:
        # Not a fatal error — repo may have no commits yet, or git not available
        log(f"Could not create rollback tag: {tag_result.stderr.strip()}")

    iteration = 0
    consecutive_failures = 0
    consecutive_no_progress = 0
    prev_commit_before = ""
    prev_commit_after = ""
    last_test_results = ""
    try:
        while True:
            iteration += 1

            if args.max_iterations and iteration > args.max_iterations:
                break

            # Re-read prompt each iteration so the user can edit it live
            user_prompt = load_prompt(prompt_path)
            scratchpad = load_scratchpad(work_dir)
            steering = load_steering(work_dir)

            # Build commit history from previous iteration
            if prev_commit_before or prev_commit_after:
                commits = git_new_commits(work_dir, prev_commit_before, prev_commit_after)
                last_commits = "\n".join(
                    f"- `{sha[:8]}` {msg.splitlines()[0]}" for sha, msg in commits
                ) if commits else ""
            else:
                last_commits = ""

            full_prompt = build_full_prompt(user_prompt, iteration, scratchpad,
                                           last_commits, steering, last_test_results)

            if steering:
                print(f">> Steering override active ({len(steering)} chars from steering.md)")
                log("Steering override injected.")

            commit_before = git_head(work_dir)

            exit_code = backend_run(
                full_prompt,
                model=args.model,
                work_dir=work_dir,
                extra_dirs=extra_dirs,
                iteration=iteration,
                idle_timeout=args.idle_timeout,
                iteration_timeout=args.iteration_timeout,
            )

            # Track consecutive failures
            if exit_code != 0:
                consecutive_failures += 1
                msg = (f"WARNING: Backend exited with code {exit_code} "
                       f"({consecutive_failures} consecutive failure(s)).")
                print(f"\n>> {msg}")
                log(msg)
                if consecutive_failures >= 3:
                    print("\n>> ERROR: 3 consecutive failures — stopping to avoid a runaway loop.")
                    log("Stopping: 3 consecutive failures.")
                    break
            else:
                consecutive_failures = 0

            # Report commit status (must run before done-signal check
            # so commits are always logged, even on the final iteration)
            commit_after = git_head(work_dir)
            if commit_after and commit_before != commit_after:
                msg = f"Agent committed (HEAD now {commit_after[:8]})"
                print(f"\n>> {msg}")
                log(msg)
                log_commits(work_dir, commit_before, commit_after)
                append_commit_log(work_dir, commit_before, commit_after)
            elif commit_after and git_has_uncommitted(work_dir):
                msg = "WARNING: Agent did NOT commit. Uncommitted changes detected."
                print(f"\n>> {msg}")
                log(msg)
            else:
                log("No commit and no uncommitted changes this iteration.")

            # Save commit info for next iteration's prompt
            prev_commit_before = commit_before
            prev_commit_after = commit_after

            # Stuck detection
            if commit_after and commit_before != commit_after:
                consecutive_no_progress = 0
            else:
                consecutive_no_progress += 1
                if consecutive_no_progress >= 3:
                    msg = (f"WARNING: No commits for {consecutive_no_progress} consecutive "
                           f"iteration(s). The agent may be stuck.")
                    print(f"\n>> {msg}")
                    log(msg)
                    # Don't break — let the operator decide via Ctrl-C or steering.md.
                    # But do reset the counter so the warning fires again after another 3.
                    consecutive_no_progress = 0

            # Check for early-exit signal
            if check_done_signal(work_dir):
                if args.test_cmd:
                    print("\n>> Agent signalled DONE — validating with test command...")
                    log("Validating done signal with test command.")
                    val_exit_code, val_output = run_test_cmd(args.test_cmd, work_dir)
                    if val_exit_code == 0:
                        msg = "Agent signalled TASK COMPLETE and tests PASSED — stopping."
                        print(f"\n>> {msg}")
                        log(msg)
                        break
                    else:
                        msg = ("Agent signalled done but tests FAILED — "
                               "continuing with failure output injected.")
                        print(f"\n>> {msg}")
                        log(msg)
                        last_test_results = (
                            f"[DONE SIGNAL REJECTED — tests failed]\n"
                            f"Command: `{args.test_cmd}`\n"
                            f"Exit code: {val_exit_code}\n\n"
                            f"```\n{val_output}\n```"
                        )
                        # Do NOT break — fall through to the delay and next iteration
                else:
                    msg = "Agent signalled TASK COMPLETE — stopping."
                    print(f"\n>> {msg}")
                    log(msg)
                    break

            # Run test command (if configured) to get feedback for next iteration
            if args.test_cmd:
                print(f"\n>> Running test command: {args.test_cmd}")
                log(f"Running test command: {args.test_cmd}")
                test_exit_code, test_output = run_test_cmd(args.test_cmd, work_dir)
                last_test_results = (
                    f"Command: `{args.test_cmd}`\n"
                    f"Exit code: {test_exit_code}\n\n"
                    f"```\n{test_output}\n```"
                )
                status = "PASSED" if test_exit_code == 0 else "FAILED"
                print(f">> Tests {status} (exit code {test_exit_code})")
                log(f"Test results ({status}):\n{test_output}")
            else:
                last_test_results = ""

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
