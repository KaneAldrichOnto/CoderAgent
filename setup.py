"""
setup.py - Configuration loading, dependency installation, and environment
           verification for CoderAgent.

Called by agent.py at startup.  Can also be run standalone to validate that
everything is ready:

    python setup.py
"""

import os
import re
import subprocess
import shutil
import sys
import platform
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AGENT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = AGENT_DIR / "CoderAgentConfig.yaml"
CONFIG_EXAMPLE = AGENT_DIR / "CoderAgentConfig.example.yaml"

_PLACEHOLDER = "XXXXXXXXXXXXXXXXXX"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


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
    """Authenticate gh CLI with the token from the config file.

    Uses ``gh auth login --with-token`` so the token is always the single
    source of truth, regardless of any prior manual authentication on this
    machine.
    """
    token = cfg.get("github_token", "")
    if not token or token == _PLACEHOLDER:
        return

    gh = shutil.which("gh")
    if not gh:
        return  # gh not installed yet — ensure_dependencies() handles this

    print("Authenticating gh CLI with config token...")
    try:
        r = subprocess.run(
            [gh, "auth", "login", "--with-token"],
            input=token,
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            print(f"WARNING: gh auth login failed: {err}", file=sys.stderr)
        else:
            print("  gh authenticated successfully.")
    except Exception as e:
        print(f"WARNING: gh auth login failed: {e}", file=sys.stderr)


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
            ("apt-get", [["apt-get", "update"],
                         ["apt-get", "install", "-y", "git"]]),
            ("dnf",     ["sudo", "dnf", "install", "-y", "git"]),
            ("pacman",  ["sudo", "pacman", "-S", "--noconfirm", "git"]),
        ],
        "gh": [
            ("apt-get", [
                ["sudo", "apt-get", "update"],
                ["sudo", "apt-get", "install", "-y", "curl"],
                "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null",
                'echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null',
                ["sudo", "apt-get", "update"],
                ["sudo", "apt-get", "install", "-y", "gh"],
            ]),
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
    """Re-read common bin directories into PATH after installs."""
    extra_dirs: list[str] = []

    for d in ("/usr/local/bin", "/usr/bin", "/usr/local/sbin"):
        if os.path.isdir(d):
            extra_dirs.append(d)

    try:
        r = subprocess.run(
            ["npm", "bin", "-g"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            extra_dirs.append(r.stdout.strip())
    except Exception:
        pass
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


def _ensure_gh_ready():
    """Verify that gh is authenticated and Copilot CLI is available."""
    gh = shutil.which("gh")
    if not gh:
        return

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

    try:
        r = subprocess.run(
            [gh, "copilot", "--", "--version"],
            capture_output=True, text=True, timeout=120,
        )
        has_copilot = r.returncode == 0
    except Exception:
        has_copilot = False

    if not has_copilot:
        print("  Downloading Copilot CLI...")
        try:
            r = subprocess.run(
                [gh, "copilot"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception:
            pass
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
        _refresh_path()
        if shutil.which(cmd) is None:
            print(f"WARNING: '{cmd}' still not found on PATH after install. "
                  f"You may need to restart your terminal.",
                  file=sys.stderr)
            sys.exit(1)

    print("All dependencies installed.\n")
    _ensure_gh_ready()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_setup():
    """Load config, apply it, and ensure all dependencies are ready."""
    cfg = load_config()
    apply_config(cfg)
    ensure_dependencies()
    return cfg


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run_setup()
    print("\nAll checks passed. CoderAgent is ready to run.")
