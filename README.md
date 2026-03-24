# CoderAgent

A lightweight wrapper that runs a [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) coding agent **in a loop** from the command line.  You write a task prompt in Markdown; the agent executes it, commits its work, and then starts the next iteration — repeating until you press **Ctrl-C**.

## Quick Start

```powershell
# 1. Edit prompt.md with your task
notepad prompt.md

# 2. Run the agent against a project
python agent.py --prompt prompt.md --dir ..\MyProject

# 3. Stop whenever you want
#    Press Ctrl-C
```

## How It Works

```
┌──────────────┐
│  prompt.md   │──(re-read each iteration)──┐
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
| **Live editing** | `prompt.md` is re-read every iteration — edit it while the agent runs to steer behavior. |
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

```powershell
# Unlimited loop, 15 s delay, two directories visible to the agent
python agent.py --prompt prompt.md --dir ..\MyProject --dir ..\SharedLib --delay 15

# Single shot
python agent.py --prompt prompt.md --dir ..\MyProject --once

# Preview the prompt without running
python agent.py --prompt prompt.md --dry-run
```

## Writing a Prompt

Open `prompt.md` (a starter template is included) and fill in four sections:

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
- **GitHub Copilot CLI** installed and authenticated (`copilot` on PATH)
- **Git** (for commit tracking)

## Logs

All output is written to `<working-dir>/logs/agent_<timestamp>.log`.  Each iteration's prompt and full Copilot response are recorded with timestamps, making it easy to review what the agent did.
