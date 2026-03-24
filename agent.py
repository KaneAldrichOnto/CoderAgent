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
import re
import time
import shutil
import argparse
import platform
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-opus-4.6"
DEFAULT_DELAY = 30  # seconds between iterations
AGENT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = AGENT_DIR / "CoderAgentConfig.yaml"
CONFIG_EXAMPLE = AGENT_DIR / "CoderAgentConfig.example.yaml"

# Force UTF-8 on Windows
if sys.platform == "win32":
    os.system("chcp 65001 > nul 2>&1")
    os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_PLACEHOLDER = "XXXXXXXXXXXXXXXXXX"


def _parse_simple_yaml(text: str) -> dict[str, str]:
    """Parse a flat key: value YAML file (no nested structures)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)", line)
        if m:
            result[m.group(1)] = m.group(2).strip().strip("'\"")
    return result


def load_config() -> dict[str, str]:
    """Load CoderAgentConfig.yaml, creating it from the example if needed.

    Returns the parsed config dict.  Exits with instructions if the token
    is still the placeholder value.
    """
    if not CONFIG_FILE.exists():
        if CONFIG_EXAMPLE.exists():
            shutil.copy2(CONFIG_EXAMPLE, CONFIG_FILE)
            print(f"Created {CONFIG_FILE.name} from example template.")
            print(f"Please edit {CONFIG_FILE} and set your GitHub token,")
            print("then re-run the agent.")
            sys.exit(1)
        else:
            print(f"ERROR: Neither {CONFIG_FILE.name} nor "
                  f"{CONFIG_EXAMPLE.name} found in {AGENT_DIR}",
                  file=sys.stderr)
            sys.exit(1)

    cfg = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))

    token = cfg.get("github_token", "")
    if not token or token == _PLACEHOLDER:
        print(f"ERROR: github_token in {CONFIG_FILE.name} is not set.")
        print(f"Please edit {CONFIG_FILE} and replace the placeholder "
              f"with your GitHub personal access token.")
        print("Generate one at: https://github.com/settings/tokens")
        sys.exit(1)

    return cfg


def apply_config(cfg: dict[str, str]):
    """Push config values into the environment so downstream tools pick them up."""
    token = cfg.get("github_token", "")
    if token and token != _PLACEHOLDER:
        os.environ["GH_TOKEN"] = token
        # Also set for git credential helpers that honour this variable
        os.environ["GITHUB_TOKEN"] = token


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------
REQUIRED_TOOLS = [
    # (command, description)
    ("git", "Git version control"),
    ("gh", "GitHub CLI (with Copilot extension)"),
]

# Per-platform install specs.
# Each tool maps to a list of (prerequisite_cmd, install_args) tried in order.
_INSTALL_SPECS = {
    "Windows": {
        "git": [("winget", ["winget", "install", "--id", "Git.Git", "-e",
                  "--accept-source-agreements", "--accept-package-agreements"])],
        "gh": [
            ("winget", ["winget", "install", "--id", "GitHub.cli", "-e",
                        "--accept-source-agreements",
                        "--accept-package-agreements"]),
        ],
    },
    "Linux": {
        "git": [
            ("apt-get", [["sudo", "apt-get", "update"],
                         ["sudo", "apt-get", "install", "-y", "git"]]),
            # Fallback without sudo (e.g. root in Docker containers)
            ("apt-get", [["apt-get", "update"],
                         ["apt-get", "install", "-y", "git"]]),
            ("dnf",     ["sudo", "dnf", "install", "-y", "git"]),
            ("pacman",  ["sudo", "pacman", "-S", "--noconfirm", "git"]),
        ],
        "gh": [
            # Install gh CLI (with sudo)
            ("apt-get", [
                ["sudo", "apt-get", "update"],
                ["sudo", "apt-get", "install", "-y", "curl"],
                "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null",
                'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null',
                ["sudo", "apt-get", "update"],
                ["sudo", "apt-get", "install", "-y", "gh"],
            ]),
            # Fallback without sudo (e.g. root in Docker containers)
            ("apt-get", [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "curl"],
                "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null",
                'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null',
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "gh"],
            ]),
            ("dnf", ["sudo", "dnf", "install", "-y", "gh"]),
        ],
    },
}


def _run_install(cmd, name: str) -> bool:
    """Run install command(s).

    *cmd* may be:
      - a single command as ``list[str]``
      - a sequence of commands as ``list[list[str] | str]`` (run in order)

    Plain ``str`` entries are executed with ``shell=True`` to support piping.
    Returns True only if every command succeeds.
    """
    # Normalise: single command → list of one
    if cmd and isinstance(cmd[0], str):
        cmds = [cmd]
    else:
        cmds = cmd

    for c in cmds:
        if isinstance(c, str):
            print(f"  Running: {c}")
            try:
                r = subprocess.run(c, shell=True, timeout=300)
            except subprocess.TimeoutExpired:
                print("ERROR: install command timed out.", file=sys.stderr)
                return False
        else:
            print(f"  Running: {' '.join(c)}")
            try:
                r = subprocess.run(c, timeout=300)
            except FileNotFoundError:
                print(f"ERROR: '{c[0]}' not found.", file=sys.stderr)
                return False
            except subprocess.TimeoutExpired:
                print("ERROR: install command timed out.", file=sys.stderr)
                return False
        if r.returncode != 0:
            return False
    return True


def _install_tool(tool: str, name: str) -> bool:
    """Install a tool using the appropriate package manager for the current OS."""
    system = platform.system()
    specs = _INSTALL_SPECS.get(system, {}).get(tool)

    if not specs:
        print(f"ERROR: No automatic install method for '{tool}' on {system}. "
              f"Please install {name} manually.", file=sys.stderr)
        return False

    tried = False
    for prereq, cmd in specs:
        if shutil.which(prereq):
            tried = True
            print(f"Installing {name} via {prereq}...")
            if _run_install(cmd, name):
                return True

    if tried:
        print(f"ERROR: All installation methods for '{tool}' failed. "
              f"Please install {name} manually.", file=sys.stderr)
    else:
        managers = ", ".join(set(prereq for prereq, _ in specs))
        print(f"ERROR: None of the supported package managers ({managers}) were found. "
              f"Please install {name} manually.", file=sys.stderr)
    return False


def _refresh_path():
    """Re-read common bin directories and npm global prefix into PATH.

    After installing new packages (e.g. nodejs via nodesource), the binaries
    may land in directories not present in the Python process's cached PATH.
    """
    extra_dirs: list[str] = []

    # Common system paths that might have been created during install
    for d in ("/usr/local/bin", "/usr/bin", "/usr/local/sbin"):
        if os.path.isdir(d):
            extra_dirs.append(d)

    # npm global bin directory
    try:
        r = subprocess.run(
            ["npm", "bin", "-g"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            extra_dirs.append(r.stdout.strip())
    except Exception:
        pass
    # Alternate method (newer npm versions)
    try:
        r = subprocess.run(
            ["npm", "prefix", "-g"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            extra_dirs.append(os.path.join(r.stdout.strip(), "bin"))
    except Exception:
        pass

    current = os.environ.get("PATH", "")
    current_set = set(current.split(os.pathsep))
    added = [d for d in extra_dirs if d not in current_set and os.path.isdir(d)]
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + current
        print(f"  Updated PATH with: {', '.join(added)}")


def ensure_dependencies():
    """Check that required CLI tools are installed; install missing ones."""
    missing = [(cmd, desc) for cmd, desc in REQUIRED_TOOLS
                if shutil.which(cmd) is None]

    if not missing:
        _ensure_gh_ready()
        return

    print("Missing dependencies detected:")
    for cmd, desc in missing:
        print(f"  - {cmd} ({desc})")
    print()

    for cmd, desc in missing:
        if not _install_tool(cmd, desc):
            sys.exit(1)
        # Refresh PATH so we can find newly-installed binaries
        _refresh_path()
        if shutil.which(cmd) is None:
            print(f"WARNING: '{cmd}' still not found on PATH after install. "
                  f"You may need to restart your terminal.",
                  file=sys.stderr)
            sys.exit(1)

    print("All dependencies installed.\n")

    _ensure_gh_ready()


def _ensure_gh_ready():
    """Verify that gh is authenticated and the copilot extension is installed.

    Prints clear, actionable instructions and exits if anything is missing.
    """
    gh = shutil.which("gh")
    if not gh:
        return  # will be caught later

    # --- Check authentication ---
    try:
        r = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        r = None

    if r is None or r.returncode != 0:
        print()
        print("=" * 60)
        print("  GitHub CLI is NOT authenticated")
        print("=" * 60)
        print()
        print("  The token in CoderAgentConfig.yaml may be invalid or expired.")
        print()
        print("  1. Generate a new token at: https://github.com/settings/tokens")
        print("     Required scope: 'copilot'")
        print(f"  2. Set github_token in {CONFIG_FILE}")
        print("  3. Re-run the agent.")
        print("=" * 60)
        sys.exit(1)

    # --- Ensure Copilot CLI is downloaded and working ---
    print("Checking Copilot CLI...")

    # First, trigger the auto-download by running 'gh copilot' bare.
    # This is what downloads the CLI binary on first use.
    try:
        r = subprocess.run(
            [gh, "copilot", "--", "--version"],
            capture_output=True, text=True, timeout=120,
        )
        has_copilot = r.returncode == 0
    except Exception:
        has_copilot = False

    if not has_copilot:
        # Trigger download explicitly with bare 'gh copilot'
        print("  Downloading Copilot CLI...")
        try:
            r = subprocess.run(
                [gh, "copilot"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            pass
        # Re-check
        try:
            r = subprocess.run(
                [gh, "copilot", "--", "--version"],
                capture_output=True, text=True, timeout=120,
            )
            has_copilot = r.returncode == 0
        except Exception:
            has_copilot = False

    if has_copilot:
        version = r.stdout.strip() or r.stderr.strip()
        if version:
            print(f"  Copilot CLI: {version}")
        return

    # Try running bare 'gh copilot' which triggers auto-download
    print("Copilot CLI not found. Triggering download via gh...")
    try:
        r = subprocess.run(
            [gh, "copilot", "--", "--help"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            print("  Copilot CLI downloaded successfully.\n")
            return
    except Exception:
        pass

    print()
    print("=" * 60)
    print("  'gh copilot' could not download the Copilot CLI")
    print("=" * 60)
    print()
    print("  gh should auto-download it, but this may fail if:")
    print("    - You are not authenticated (run: gh auth login)")
    print("    - Your architecture is not amd64 or arm64")
    print("    - Network issues prevented the download")
    print()
    print("  Try running manually to see the error:")
    print("      gh copilot")
    print("=" * 60)
    sys.exit(1)

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
                iteration: int) -> int:
    """Invoke the Copilot CLI with the given prompt.

    Returns the process exit code.
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        output_lines = []
        for line in proc.stdout:
            print(line, end="")
            log(line.rstrip())
            output_lines.append(line)

        proc.wait()
        output = "".join(output_lines)
        log_section(f"OUTPUT (iteration {iteration})", output)

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

    args = parser.parse_args()

    # Load config (token, etc.) and push into environment
    cfg = load_config()
    apply_config(cfg)

    # Auto-install missing dependencies
    ensure_dependencies()

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
