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
4. **Install missing dependencies** (`git`, `gh`) via `winget` (Windows) or `apt`/`dnf` (Linux).
5. **Download the Copilot CLI** binary from `github/copilot-cli` releases if not already present.

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

When running inside a Docker container (e.g., the included `ModelTraining/.devcontainer/Dockerfile`):

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
| `--model NAME` | `claude-opus-4.6` | Copilot model. |
| `--delay SECONDS` | `30` | Pause between iterations. |
| `--max-iterations N` | `0` (unlimited) | Stop after N iterations. |
| `--once` | — | Shorthand for `--max-iterations 1`. |
| `--dry-run` | — | Print the full prompt and exit. |

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

Everything else (`git`, `gh` CLI, Copilot CLI binary) is installed automatically by `setup.py` on first run.

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
