# CoderAgent

A lightweight wrapper that runs a [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) coding agent **in a loop** from the command line.  You write a task prompt in Markdown; the agent executes it, commits its work, and then starts the next iteration — repeating until you press **Ctrl-C**.

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/KaneAldrichOnto/CoderAgent.git
cd CoderAgent

# 2. Set up your GitHub token (see "Setup" below)
#    This creates CoderAgentConfig.yaml from the template
python setup.py

# 3. Create your prompt (auto-copies from prompt.example.md on first run)
#    Just run the agent — it will create prompt.md and ask you to edit it
python agent.py --prompt prompt.md --dir ../MyProject

# 4. Edit prompt.md with your task
#    Windows: notepad prompt.md
#    Linux:   nano prompt.md

# 5. Run the agent again
python agent.py --prompt prompt.md --dir ../MyProject

# 5. Stop whenever you want
#    Press Ctrl-C
```

## Setup

On first run, `setup.py` (called automatically by `agent.py`) will:

1. **Create `CoderAgentConfig.yaml`** from the example template if it doesn't exist.
2. **Validate your token** — rejects classic PATs (`ghp_`) early with a clear message.
3. **Set `GH_TOKEN`** in the environment and authenticate the GitHub CLI.
4. **Install missing core dependencies** (`git`, `gh`) via `winget` (Windows) or `apt`/`dnf`/`pacman` (Linux).
5. **Download the Copilot CLI** binary from `github/copilot-cli` releases if not already present.
6. **Install the optional feature dependencies** so every CoderAgent capability
   works out of the box (see [Auto-installed optional dependencies](#auto-installed-optional-dependencies) below):
   - `pywinauto` + `pillow` (Python, via `pip`) — required by `gui_nav.py` and the
     image-edit subcommands of `doc_tools.py`. `pywinauto` is Windows-only and is
     skipped with a notice elsewhere; `pillow` installs everywhere.
   - `@mermaid-js/mermaid-cli` (Node, via `npm install -g`) — required by
     `doc_tools.py render-diagram`. Node.js itself is auto-installed if missing.
   - `@github/copilot` npm CLI — the standalone multimodal CLI used by
     `agent.py --cli copilot`. Installed when `--backend=copilot` (the default).

Pass `--no-gui-deps` and/or `--no-copilot-npm-cli` to `agent.py` if you want
the minimal install behaviour. Each optional install degrades gracefully
(prints a clear message and continues) if its package manager isn't
available; the core loop is never blocked by an optional dep.

### Generating a GitHub Token

> **Important:** The Copilot CLI does **not** accept classic personal access tokens (`ghp_`).
> You **must** use a **Fine-Grained Personal Access Token** (`github_pat_`).

1. Go to **https://github.com/settings/personal-access-tokens/new**.
2. Give it a descriptive name (e.g., `CoderAgent`).
3. Set an expiration date.
4. Under **Account permissions**, enable:
   - **GitHub Copilot** → **Read-only**
5. Click **Generate token** and copy it.
6. Paste the token into `CoderAgentConfig.yaml`:
   ```yaml
   github_token: github_pat_your_token_here
   ```

> **Note:** `CoderAgentConfig.yaml` is git-ignored — your token will not be committed.

You can also run `python setup.py` standalone to verify everything is configured correctly without starting the agent loop.

## Platform Support

The setup process is fully automated on both Windows and Linux (including Docker containers).

| Step | Windows | Linux / Docker |
|---|---|---|
| Install `git` | `winget` | `apt-get` / `dnf` / `pacman` (with and without `sudo`) |
| Install `gh` CLI | `winget` | GitHub's official APT repo |
| Download Copilot CLI | `copilot-win32-x64.zip` → `%LOCALAPPDATA%\Programs\copilot` | `copilot-linux-x64.tar.gz` → `/usr/local/bin` (or `~/.local/bin`) |
| Auth | `gh auth login --with-token` + `GH_TOKEN` env var | Same (env var ensures it works without a keyring) |

### Running in Docker

When running inside a Docker container:

- The `GH_TOKEN` environment variable is set **before** `gh` is installed, so authentication works immediately after install.
- `apt-get` commands run without `sudo` (root in container) — this is handled automatically.
- The Copilot CLI is downloaded directly via `gh release download` since `gh copilot`'s auto-download doesn't work in non-interactive mode.

## How It Works

```
┌─────────────────────┐
│ prompt.example.md   │──(copied to prompt.md on first run)
└─────────────────────┘
         │
         ▼
┌──────────────┐
│  prompt.md   │──(re-read each iteration)──┐
│  (git-ignored)│                            │
└──────────────┘                             │
                                             ▼
                                   ┌──────────────────┐
                                   │   agent.py loop   │
                                   │                    │
                                   │  1. Read prompt    │
                                   │  2. Invoke Copilot │◄──┐
                                   │  3. Stream output   │   │
                                   │  4. Check commits   │   │
                                   │  5. Wait & repeat  ─┼───┘
                                   └──────────────────┘
                                          │
                                          ▼
                                   ┌──────────────┐
                                   │  logs/        │
                                   │  agent_*.log  │
                                   └──────────────┘
```

### Key behaviors

| Feature | Detail |
|---|---|
| **Loop** | Runs indefinitely until Ctrl-C (or `--max-iterations N`). |
| **Live editing** | `prompt.md` is re-read every iteration — edit it while the agent runs to steer behavior. Your prompts are git-ignored and never committed. |
| **Logging** | Full prompts and Copilot output are saved to `logs/agent_<timestamp>.log` inside the working directory. |
| **Commit tracking** | After each iteration the script checks `git log` and warns if the agent didn't commit. |
| **Housekeeping wrapper** | A small wrapper is appended to your prompt reminding the agent to commit regularly and stay on task. |

## Usage

```
python agent.py --prompt <FILE> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--prompt FILE` | *(required)* | Markdown file with your task description. |
| `--dir PATH` | current dir | Directory to expose to the agent. First `--dir` is the working directory. Repeatable. |
| `--model NAME` | `claude-opus-4.7` | Model name passed to the underlying CLI. |
| `--delay SECONDS` | `30` | Pause between iterations. |
| `--max-iterations N` | `0` (unlimited) | Stop after N iterations. |
| `--once` | — | Shorthand for `--max-iterations 1`. |
| `--dry-run` | — | Print the full prompt and exit. |
| `--idle-timeout SECONDS` | `300` | Kill the iteration if the CLI produces no output for this many seconds. `0` to disable. |
| `--iteration-timeout SECONDS` | `3600` | Kill the iteration after this many total seconds. `0` to disable. |
| `--test-cmd CMD` | — | Shell command to run after each iteration (e.g. `pytest tests/`). Output is injected into the next iteration's prompt and used to validate the `.agent_done` signal. |
| `--backend {copilot,claude}` | `copilot` | Which agent CLI to drive. |
| `--cli {copilot,gh-copilot,auto}` | `auto` | When `--backend=copilot`, pick the new standalone `copilot` CLI (supports in-turn vision) or the legacy `gh copilot` extension. `auto` prefers `copilot` if on PATH. |
| `--no-gui-deps` | — | Skip the auto-install of `pywinauto` (Windows) and `pillow` during setup. The features that need them just print an install hint instead. |
| `--no-copilot-npm-cli` | — | When `--backend=copilot`, skip the auto-install of the `@github/copilot` npm CLI. The loop falls back to legacy `gh copilot` if available. |

### Examples

```bash
# Unlimited loop, 15 s delay, two directories visible to the agent
python agent.py --prompt prompt.md --dir ../MyProject --dir ../SharedLib --delay 15

# Single shot
python agent.py --prompt prompt.md --dir ../MyProject --once

# Preview the prompt without running
python agent.py --prompt prompt.md --dry-run
```

## Writing a Prompt

The repo includes `prompt.example.md` — a starter template with placeholder sections.
**Your actual prompts go in `prompt.md`**, which is git-ignored and never committed.

### How it works

1. On first run, if `prompt.md` doesn't exist, the agent **automatically copies
   `prompt.example.md` → `prompt.md`** and exits, asking you to edit it.
2. You fill in your task in `prompt.md` and run the agent again.
3. `prompt.md` is re-read every iteration, so you can edit it while the agent runs.
4. Since `prompt.md` is git-ignored, your task-specific prompts stay local and are
   never pushed to the repo.

> **Note:** Only `prompt.example.md` is committed. If you want to improve the
> template for everyone, edit `prompt.example.md`. Never put sensitive or
> project-specific details in the example file.

Fill in four sections in your prompt:

1. **Goal** — what the agent should accomplish.
2. **Context** — which files/directories to read first.
3. **Steps** — numbered workflow the agent should follow.
4. **Rules** — hard constraints (e.g., "don't modify tests", "always run linter before committing").

The prompt is plain Markdown — include code blocks, links, file paths, or anything else the model can use.

### Tips

- **Be specific.** "Fix the null-pointer crash in `src/parser.cpp` line 42" works better than "fix bugs".
- **Include build/test commands.** Tell the agent exactly how to build and verify: `dotnet test`, `npm test`, `.\scripts\build.ps1 -Test`, etc.
- **Steer mid-flight.** Edit `prompt.md` while the loop is running — the next iteration picks up your changes.

## Prerequisites

- **Python 3.10+**
- **A GitHub account** with Copilot access
- **A Fine-Grained Personal Access Token** (`github_pat_`) — classic tokens (`ghp_`) are **not supported**
- **Windows:** `winget` (ships with Windows 10/11) — used to auto-install `git` and `gh` if missing
- **Linux:** `apt-get`, `dnf`, or `pacman` — used to auto-install `git` and `gh` if missing

The core loop installs everything else (`git`, `gh` CLI, Copilot CLI binary)
automatically via `setup.py` on first run.

### Auto-installed optional dependencies

Unless you opt out with `--no-gui-deps` / `--no-copilot-npm-cli`, `agent.py`
also installs the dependencies that power the optional features below the
first time it runs. Everything is detected before being installed, so
subsequent runs are no-ops.

| Dependency | Auto-install method | Used by | Platform |
|---|---|---|---|
| `@github/copilot` npm CLI | `npm install -g @github/copilot` (installs Node.js first if missing) | `agent.py --cli copilot` — enables in-turn multimodal vision | Windows + Linux |
| `pywinauto` (Python) | `pip install pywinauto` | `gui_nav.py` GUI automation server | **Windows only** (skipped elsewhere with a notice) |
| `pillow` (Python) | `pip install pillow` | `doc_tools.py` image crop / annotate / arrow / label | Windows + Linux |
| `@mermaid-js/mermaid-cli` (`mmdc`) | `npm install -g @mermaid-js/mermaid-cli` (installs Node.js first if missing) | `doc_tools.py render-diagram` (Mermaid → PNG) | Windows + Linux |

Manual install is still supported for any dependency — use the `npm` /
`pip` command from the table above. You can also run them as a group via
`setup.py` standalone:

```bash
python setup.py --with-gui                       # pywinauto (Win) + pillow + mmdc
python setup.py --backend copilot --install-copilot-cli   # @github/copilot CLI
python setup.py --with-gui --backend copilot --install-copilot-cli  # everything
```

## Optional features

All optional, all opt-in *from a workflow perspective*. The dependencies are
auto-installed by `setup.py` on first run (see
[Auto-installed optional dependencies](#auto-installed-optional-dependencies)),
so every feature below works out of the box — you just decide whether to use
it. The core loop runs unchanged whether you use any of them or not.

| Feature | What it does | How to use |
|---|---|---|
| **Scratchpad round-trip** | The loop reads `agent_scratchpad.md` from the working directory at the start of each iteration and embeds it into the prompt under a clearly-labeled section. The model is asked to overwrite the file before stopping. Git-ignored. | Just run the loop — it's on by default. The model decides when to write the file. |
| **`.agent_done` sentinel** | The model creates an empty `.agent_done` file in the working directory when its task is complete; the loop deletes it and exits. | On by default. Combine with `--test-cmd` to require a passing test before honoring the signal. |
| **Internal commit logs** | Per-commit text files in `InternalLogs/` (timestamped, sha-stamped) plus a rolling human-readable `commit_log.md` (subject, body, files changed, diff stat, truncated patch). | On by default. Both paths are git-ignored. |
| **Idle / iteration timeouts** | Kill the CLI subprocess if it produces no output for `--idle-timeout` seconds (default 300) or runs longer than `--iteration-timeout` seconds (default 3600). | Tune via flags. Pass `0` to disable either. |
| **In-turn vision** | The new `copilot` CLI is multimodal: the model can open image files directly mid-turn (no MCP shim, no `@`-attach). Captures and annotations from `doc_tools.py` are immediately readable. | `python agent.py --backend copilot --cli copilot ...` |
| **GUI automation (`gui_nav.py`)** | Persistent TCP UIA server holding a `pywinauto` connection alive between calls. Sub-second `find` / `click` / `screenshot` from the loop. Windows-only. | `python gui_nav.py serve --process MyApp.exe`, then call `python gui_nav.py click "OK"` etc. from your prompt. |
| **Doc toolkit (`doc_tools.py`)** | App-agnostic image crop / annotate-rect / annotate-arrow / annotate-label / Mermaid → PNG, plus thin wrappers around `gui_nav.py screenshot` and a `capture` (click + screenshot + save) shortcut. | `python doc_tools.py --help` for the full list. |

## App overlays (downstream wrappers)

CoderAgent is meant to be **wrapped, not forked**. A downstream project
that needs GUI automation, screenshot pipelines, or domain-specific
document readers ships its own toolkit script and its own rolling
`agent_context.md` alongside it, and references both from its own
`prompt.md`. The toolkit script calls into `gui_nav.py` and
`doc_tools.py` for the heavy lifting; only the app-specific bits
(target process name, document layout, alias map) live downstream.

A typical overlay layout:

```
my-app-docs/
├── agent/
│   ├── prompt.md                  # references ../agent_context.md
│   ├── agent_context.md           # rolling app-specific context
│   ├── my_app_agent.py            # subcommand toolkit (HTML/PPTX/etc.)
│   └── MyAppConfig.yaml           # target_process: MyApp.exe
└── coder-agent/                   # this repo, vendored or submodule
    ├── agent.py
    ├── gui_nav.py
    └── doc_tools.py
```

Run from the overlay:

```bash
python ../coder-agent/agent.py \
  --prompt prompt.md \
  --dir . --dir ../coder-agent \
  --backend copilot --cli copilot
```

Nothing in this repo references your overlay — that's the point.

## Troubleshooting

| Problem | Solution |
|---|---|
| `Classic Personal Access Tokens (ghp_) are not supported` | Replace your token with a fine-grained PAT from https://github.com/settings/personal-access-tokens/new |
| `GitHub CLI is NOT authenticated` | Your token may be invalid or expired. Generate a new one and update `CoderAgentConfig.yaml`. |
| `Could not install the Copilot CLI` | Run `gh release download -R github/copilot-cli -p copilot-linux-x64.tar.gz` manually, extract, and place `copilot` on your PATH. |
| `Permission denied` writing files | Inside Docker you're root; on the host, ensure your user owns the CoderAgent directory: `sudo chown -R $(whoami) .` |
| `gh auth login` fails in Docker | The `GH_TOKEN` env var is set automatically from your config. If it still fails, check your token hasn't expired. |

## Logs

All output is written to `<working-dir>/logs/agent_<timestamp>.log`.  Each iteration's prompt and full Copilot response are recorded with timestamps, making it easy to review what the agent did.
