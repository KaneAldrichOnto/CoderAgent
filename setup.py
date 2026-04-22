"""
setup.py - Configuration loading, dependency installation, and environment
           verification for CoderAgent.

Called by agent.py at startup.  Can also be run standalone to validate that
everything is ready:

    python setup.py                       # claude backend (default)
    python setup.py --backend copilot     # legacy gh copilot backend
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
        print("Generate one at: https://github.com/settings/personal-access-tokens/new")
        sys.exit(1)

    if token.startswith("ghp_"):
        print("=" * 60)
        print("  WARNING: Classic PATs (ghp_) are NOT supported by the Copilot CLI.")
        print("  Please replace your token with a Fine-Grained PAT (github_pat_).")
        print()
        print("  Generate one at: https://github.com/settings/personal-access-tokens/new")
        print("  Required: Account permissions → GitHub Copilot → Read-only")
        print(f"  Update: {CONFIG_FILE}")
        print("=" * 60)
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

    # Always set GH_TOKEN so it's available even if gh is installed later
    os.environ["GH_TOKEN"] = token

    gh = shutil.which("gh")
    if not gh:
        return  # gh not installed yet — ensure_dependencies() handles this

    print("Authenticating gh CLI with config token...")
    try:
        r = subprocess.run(
            [gh, "auth", "login", "--hostname", "github.com",
             "--git-protocol", "https", "--with-token"],
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
        "node": [
            ("winget", ["winget", "install", "--id", "OpenJS.NodeJS.LTS", "-e",
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
        "node": [
            ("apt-get", [
                ["sudo", "apt-get", "update"],
                ["sudo", "apt-get", "install", "-y", "nodejs", "npm"],
            ]),
            ("apt-get", [
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "nodejs", "npm"],
            ]),
            ("dnf", ["sudo", "dnf", "install", "-y", "nodejs", "npm"]),
            ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "nodejs", "npm"]),
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
        print("  NOTE: Classic PATs (ghp_) are NOT supported by the Copilot CLI.")
        print("  You must use a Fine-Grained PAT (github_pat_).")
        print()
        print("  1. Go to: https://github.com/settings/personal-access-tokens/new")
        print("  2. Under Account permissions, enable: GitHub Copilot → Read-only")
        print(f"  3. Set github_token in {CONFIG_FILE}")
        print("  4. Re-run the agent.")
        print("=" * 60)
        sys.exit(1)

    # --- Ensure Copilot CLI is downloaded and working ---
    print("Checking Copilot CLI...")

    try:
        r = subprocess.run(
            [gh, "copilot", "--", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        has_copilot = r.returncode == 0
    except Exception:
        has_copilot = False

    if has_copilot:
        version = r.stdout.strip() or r.stderr.strip()
        if version:
            print(f"  Copilot CLI: {version}")
        return

    # gh copilot auto-download is broken in non-interactive mode.
    # Download the binary ourselves from github/copilot-cli releases.
    print("  Copilot CLI not found. Downloading from github/copilot-cli...")
    _install_copilot_cli(gh)

    # Verify
    try:
        r = subprocess.run(
            [gh, "copilot", "--", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            version = r.stdout.strip() or r.stderr.strip()
            print(f"  Copilot CLI: {version}")
            return
    except Exception:
        pass

    print()
    print("=" * 60)
    print("  Could not install the Copilot CLI")
    print("=" * 60)
    print()
    print("  Try installing manually:")
    if platform.system() == "Windows":
        print("      gh release download -R github/copilot-cli -p copilot-win32-x64.zip")
        print("      Expand-Archive copilot-win32-x64.zip .")
        print("      Move copilot.exe to a directory on your PATH")
    else:
        print("      gh release download -R github/copilot-cli -p copilot-linux-x64.tar.gz")
        print("      tar xzf copilot-linux-x64.tar.gz")
        print("      mv copilot /usr/local/bin/copilot && chmod +x /usr/local/bin/copilot")
    print("=" * 60)
    sys.exit(1)


def _install_copilot_cli(gh: str):
    """Download the Copilot CLI binary from github/copilot-cli releases."""
    import tempfile
    import zipfile

    machine = platform.machine().lower()
    system = platform.system().lower()

    arch_map = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    arch = arch_map.get(machine)
    if not arch:
        print(f"  ERROR: Unsupported architecture: {machine}", file=sys.stderr)
        return

    if system == "linux":
        asset = f"copilot-linux-{arch}.tar.gz"
    elif system == "darwin":
        asset = f"copilot-darwin-{arch}.tar.gz"
    elif system == "windows":
        asset = f"copilot-win32-{arch}.zip"
    else:
        print(f"  ERROR: Unsupported platform: {system}", file=sys.stderr)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"  Downloading {asset}...")
        try:
            r = subprocess.run(
                [gh, "release", "download", "--repo", "github/copilot-cli",
                 "--pattern", asset, "--dir", tmpdir],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                err = r.stderr.strip() or r.stdout.strip()
                print(f"  ERROR: Download failed: {err}", file=sys.stderr)
                return
        except Exception as e:
            print(f"  ERROR: Download failed: {e}", file=sys.stderr)
            return

        asset_path = Path(tmpdir) / asset

        # Extract
        if asset.endswith(".tar.gz"):
            subprocess.run(["tar", "xzf", str(asset_path), "-C", tmpdir],
                           timeout=30)
        elif asset.endswith(".zip"):
            with zipfile.ZipFile(str(asset_path), "r") as zf:
                zf.extractall(tmpdir)

        binary_name = "copilot.exe" if system == "windows" else "copilot"
        binary = Path(tmpdir) / binary_name

        if not binary.exists():
            print("  ERROR: Expected binary not found after extraction",
                  file=sys.stderr)
            return

        # Choose install directory
        if system == "windows":
            # %LOCALAPPDATA%\Programs\copilot  (user-local, no admin needed)
            install_dir = Path(os.environ.get("LOCALAPPDATA",
                               Path.home() / "AppData" / "Local")) / "Programs" / "copilot"
        else:
            install_dir = Path("/usr/local/bin")
            if not os.access(str(install_dir), os.W_OK):
                install_dir = Path.home() / ".local" / "bin"

        install_dir.mkdir(parents=True, exist_ok=True)
        dest = install_dir / binary_name
        shutil.copy2(str(binary), str(dest))
        if system != "windows":
            dest.chmod(0o755)
        print(f"  Installed copilot to {dest}")

        # Ensure the install dir is on PATH
        if str(install_dir) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(install_dir) + os.pathsep + os.environ.get("PATH", "")
            print(f"  Added {install_dir} to PATH")


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
# Claude backend setup
# ---------------------------------------------------------------------------

def _install_claude_cli() -> bool:
    """Install the Claude Code CLI via npm."""
    npm = shutil.which("npm")
    if not npm:
        print("  ERROR: npm not found. Cannot install Claude Code CLI.", file=sys.stderr)
        return False
    print("  Installing Claude Code CLI via npm (this may take a minute)...")
    try:
        r = subprocess.run(
            [npm, "install", "-g", "@anthropic-ai/claude-code"],
            timeout=300,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print("  ERROR: npm install timed out.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: npm install failed: {e}", file=sys.stderr)
        return False


def _ensure_python_package(package: str) -> bool:
    """Install a Python package via pip if not already importable."""
    import_name = package.replace("-", "_")
    try:
        import importlib
        importlib.import_module(import_name)
        return True  # already installed
    except ImportError:
        pass

    print(f"  Installing Python package: {package}...")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            timeout=120,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ERROR: pip install {package} timed out.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: pip install {package} failed: {e}", file=sys.stderr)
        return False


def _check_anthropic_api_key():
    """Ensure ANTHROPIC_API_KEY is set; load from config file if available."""
    # Already set in environment — nothing to do
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  ANTHROPIC_API_KEY found in environment.")
        return

    # Try loading from CoderAgentConfig.yaml if it has the key
    if CONFIG_FILE.exists():
        cfg = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))
        key = cfg.get("anthropic_api_key", "")
        if key and key != _PLACEHOLDER and not key.startswith("sk-ant-XXXX"):
            os.environ["ANTHROPIC_API_KEY"] = key
            print("  ANTHROPIC_API_KEY loaded from CoderAgentConfig.yaml.")
            return

    print()
    print("=" * 60)
    print("  WARNING: ANTHROPIC_API_KEY is not set")
    print("=" * 60)
    print()
    print("  The Claude backend requires an Anthropic API key.")
    print()
    print("  Option 1 — Environment variable (recommended):")
    if platform.system() == "Windows":
        print("      $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
    else:
        print("      export ANTHROPIC_API_KEY=sk-ant-...")
    print()
    print("  Option 2 — CoderAgentConfig.yaml:")
    print("      anthropic_api_key: sk-ant-...")
    print()
    print("  Get a key at: https://console.anthropic.com/")
    print("=" * 60)
    print()
    # Warn but don't exit — the user might set it via another mechanism
    # (e.g., claude CLI keychain, system-level env var not yet visible)


def _find_npm_on_windows() -> str:
    """Search standard Windows Node.js install locations for npm.cmd.

    Returns the directory containing npm.cmd if found, else empty string.
    winget/chocolatey may install Node but not update the current session's
    PATH, so a disk search is needed before concluding npm is absent.
    """
    prog_files = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
    ]
    candidates: list[Path] = []

    # Standard installer locations
    for pf in prog_files:
        if pf:
            candidates.append(Path(pf) / "nodejs")

    # nvm-windows: %APPDATA%\nvm\<version>
    appdata = os.environ.get("APPDATA", "")
    nvm_root = os.environ.get("NVM_HOME", os.path.join(appdata, "nvm") if appdata else "")
    if nvm_root and Path(nvm_root).is_dir():
        for child in sorted(Path(nvm_root).iterdir(), reverse=True):
            if child.is_dir() and (child / "npm.cmd").exists():
                return str(child)

    # Chocolatey
    choco = Path(r"C:\ProgramData\chocolatey\bin")
    if choco.is_dir():
        candidates.append(choco)

    for candidate in candidates:
        if (candidate / "npm.cmd").exists() or (candidate / "npm").exists():
            return str(candidate)

    return ""


def _ensure_claude_ready():
    """Install Node.js, Claude Code CLI, and verify API key."""
    system = platform.system()

    # Step 1: Ensure Node.js / npm is available (required for claude CLI)
    if not shutil.which("npm"):
        # On Windows, Node may be installed but not on the current session PATH.
        # Search known locations before attempting (and potentially failing on)
        # a fresh winget install — winget returns non-zero when nothing to upgrade.
        found_dir = _find_npm_on_windows() if system == "Windows" else ""
        if found_dir:
            os.environ["PATH"] = found_dir + os.pathsep + os.environ.get("PATH", "")
            print(f"Node.js/npm found at {found_dir} — added to PATH.")
        else:
            print("Node.js/npm not found — required for Claude Code CLI.")
            _install_tool("node", "Node.js LTS")
            _refresh_path()
            # Search again in case the installer put it in a non-standard spot
            if not shutil.which("npm") and system == "Windows":
                found_dir = _find_npm_on_windows()
                if found_dir:
                    os.environ["PATH"] = found_dir + os.pathsep + os.environ.get("PATH", "")
            if not shutil.which("npm"):
                print()
                print("  npm still not found after install.")
                print("  Please install Node.js from https://nodejs.org/ then re-run.")
                sys.exit(1)
    else:
        print("Node.js/npm found.")

    # Step 2: Ensure Claude Code CLI is installed
    if not shutil.which("claude"):
        print("Claude Code CLI not found — installing...")
        if not _install_claude_cli():
            print()
            print("  Please install Claude Code manually:")
            print("      npm install -g @anthropic-ai/claude-code")
            print("  Docs: https://docs.anthropic.com/en/docs/claude-code")
            sys.exit(1)
        _refresh_path()

        # On Windows the npm global bin may not be on PATH yet in this process;
        # try common locations before giving up.
        if not shutil.which("claude") and system == "Windows":
            appdata = os.environ.get("APPDATA", "")
            npm_global = os.path.join(appdata, "npm") if appdata else ""
            if npm_global and os.path.isdir(npm_global):
                os.environ["PATH"] = npm_global + os.pathsep + os.environ.get("PATH", "")

        if not shutil.which("claude"):
            print("WARNING: 'claude' not found on PATH after install. "
                  "You may need to open a new terminal.", file=sys.stderr)
            sys.exit(1)

    # Verify claude version
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        version = r.stdout.strip() or r.stderr.strip()
        print(f"Claude Code CLI: {version or 'installed'}")
    except Exception:
        print("Claude Code CLI: installed (version check failed)")

    # Step 3: Ensure claude-agent-sdk Python package is available
    print("Checking claude-agent-sdk Python package...")
    if not _ensure_python_package("claude-agent-sdk"):
        print("WARNING: Could not install claude-agent-sdk. "
              "Run: pip install claude-agent-sdk", file=sys.stderr)
        # Non-fatal: agent.py subprocess mode still works without the SDK
    else:
        print("  claude-agent-sdk ready.")

    # Step 4: Verify Anthropic API key
    _check_anthropic_api_key()


# ---------------------------------------------------------------------------
# Optional npm CLI: @github/copilot
# ---------------------------------------------------------------------------

def _install_npm_global(package: str) -> bool:
    """Install a package globally via npm. Returns True on success."""
    npm = shutil.which("npm")
    if not npm:
        print(f"  ERROR: npm not found. Cannot install {package}.",
              file=sys.stderr)
        return False
    print(f"  Installing {package} via npm (this may take a minute)...")
    try:
        r = subprocess.run([npm, "install", "-g", package], timeout=300)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  ERROR: npm install {package} timed out.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: npm install {package} failed: {e}", file=sys.stderr)
        return False


def _ensure_copilot_npm_cli():
    """Detect the new ``@github/copilot`` npm CLI, optionally installing it.

    The standalone ``copilot`` CLI (``npm i -g @github/copilot``) is what
    enables in-turn multimodal vision. If it is not on PATH, attempt a
    one-shot install via npm so the next loop iteration can pick it up.
    Falls back silently if npm is missing -- agent.py will still work via
    ``--cli gh-copilot``.
    """
    if shutil.which("copilot"):
        try:
            r = subprocess.run(["copilot", "--version"],
                               capture_output=True, text=True, timeout=15)
            ver = (r.stdout or r.stderr).strip()
            print(f"  @github/copilot CLI: {ver or 'installed'}")
        except Exception:
            print("  @github/copilot CLI: installed")
        return True

    if not shutil.which("npm"):
        print("  @github/copilot CLI not present; npm not found either. "
              "Falling back to legacy 'gh copilot'.")
        return False

    print("  @github/copilot CLI not found -- installing...")
    if not _install_npm_global("@github/copilot"):
        print("  WARNING: could not install @github/copilot. "
              "agent.py will fall back to 'gh copilot' if available.",
              file=sys.stderr)
        return False
    _refresh_path()
    return shutil.which("copilot") is not None


# ---------------------------------------------------------------------------
# Optional GUI feature deps (pywinauto + pillow), opt in via --with-gui
# ---------------------------------------------------------------------------
GUI_PYTHON_PACKAGES = [
    # (pip_name, import_name)
    ("pywinauto", "pywinauto"),
    ("pillow", "PIL"),
]


def install_gui_dependencies() -> bool:
    """Install pywinauto + pillow on Windows. No-op (with notice) elsewhere.

    Returns True on success (or when nothing needed installing).
    """
    if platform.system() != "Windows":
        print()
        print("  GUI features (gui_nav.py / doc_tools screenshot) are "
              "Windows-only.")
        print("  Skipping pywinauto install on this platform.")
        # Still try to install Pillow for the image-edit subcommands.
        try:
            __import__("PIL")
            print("  pillow: already installed")
        except ImportError:
            print("  Installing pillow for image-edit subcommands...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet",
                     "pillow"], check=True, timeout=180,
                )
            except Exception as e:
                print(f"  WARNING: pip install pillow failed: {e}",
                      file=sys.stderr)
        return False

    missing = []
    for pip_name, import_name in GUI_PYTHON_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        print("  GUI Python packages: all present (pywinauto, pillow)")
        return True

    print(f"  Installing GUI packages: {', '.join(missing)}")
    for args in (
        [sys.executable, "-m", "pip", "install", "--quiet", *missing],
        [sys.executable, "-m", "pip", "install", "--quiet", "--user",
         *missing],
    ):
        try:
            r = subprocess.run(args, timeout=300)
            if r.returncode == 0:
                print("  GUI packages installed.")
                return True
        except Exception:
            continue
    print(f"  ERROR: could not install: {missing}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_setup(backend: str = "claude", *, with_gui: bool = False,
              install_copilot_cli: bool = False):
    """Load config, apply it, and ensure all dependencies are ready.

    When backend='claude' (default): installs Node.js, Claude Code CLI, and
    claude-agent-sdk automatically.  No GitHub token required.

    When backend='copilot': uses the original gh/Copilot setup flow.

    When install_copilot_cli=True (and backend='copilot'): also detect /
    install the new standalone ``@github/copilot`` npm CLI, which enables
    in-turn vision via ``agent.py --cli copilot``.

    When with_gui=True: install the optional GUI automation deps
    (pywinauto + pillow on Windows; pillow only elsewhere).
    """
    if backend == "claude":
        _ensure_claude_ready()
        cfg = {}
    else:
        # Legacy copilot backend: full gh setup
        cfg = load_config()
        apply_config(cfg)
        ensure_dependencies()
        if install_copilot_cli:
            _ensure_copilot_npm_cli()

    if with_gui:
        install_gui_dependencies()

    return cfg


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse as _argparse
    _p = _argparse.ArgumentParser(description="Verify CoderAgent dependencies")
    _p.add_argument("--backend", choices=["claude", "copilot"],
                    default="claude")
    _p.add_argument("--with-gui", action="store_true",
                    help="Also install pywinauto + pillow for the optional "
                         "gui_nav.py / doc_tools image features (Windows "
                         "only for pywinauto; pillow installed everywhere).")
    _p.add_argument("--install-copilot-cli", action="store_true",
                    help="With --backend=copilot, also install the new "
                         "@github/copilot npm CLI (enables in-turn vision).")
    _args = _p.parse_args()
    run_setup(_args.backend,
              with_gui=_args.with_gui,
              install_copilot_cli=_args.install_copilot_cli)
    print("\nAll checks passed. CoderAgent is ready to run.")
