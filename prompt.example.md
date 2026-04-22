# Task Prompt
<!-- ================================================================
     Write your task here. The agent will read this file at the start
     of every iteration, so you can edit it while the agent is running
     to steer its behavior.
     ================================================================ -->

## Goal

<!-- Describe what you want the agent to accomplish.  Be specific:
     which files to touch, what the end result should look like, and
     any constraints the agent should follow. -->



## Context

<!-- Point the agent at the key files and directories it should read
     first.  Example:
       - Read `src/AGENTS.md` for project conventions
       - Implementation is in `src/engine/`
       - Tests are in `tests/engine/`
-->



## Steps

<!-- Give a numbered workflow.  Example:
     1. Read the existing tests and run them to see what passes.
     2. Identify gaps in test coverage.
     3. Write new tests for uncovered edge cases.
     4. Fix any bugs the new tests reveal.
     5. Commit after each fix with a clear message.
-->

1. 
2. 
3. 

## Rules

<!-- Any hard constraints.  Example:
     - Do not modify files outside `src/engine/`
     - All tests must pass before committing
     - Use incremental builds, never clean-build
-->

- Commit after every meaningful change with a descriptive message.
- Do not refactor code that is unrelated to the task.

## Optional capabilities (opt in only if you actually need them)

The agent loop ships with several optional features. They are off by
default — only mention them in *Steps* / *Rules* if your task actually
needs them.

- **Scratchpad round-trip** — the loop writes `agent_scratchpad.md` from
  the previous iteration into the next iteration's prompt. Use it to
  carry forward "Done", "Next", and "Open questions" between turns.
- **`.agent_done` sentinel** — when your task is fully complete, create
  an empty `.agent_done` file in the working directory; the loop will
  exit cleanly.
- **Internal commit logs** — every commit is logged to `InternalLogs/`
  and appended to a human-readable `commit_log.md`. Both are git-ignored.
- **In-turn vision** — when invoked with the new `copilot` CLI, the
  model can open image files (PNGs, screenshots) directly in the same
  turn. No MCP shim required. Just reference the path.
- **`gui_nav.py` GUI automation** — drive arbitrary Windows GUI apps via
  a persistent UIA server. Configure the target process via `--process`
  or `target_process:` in `CoderAgentConfig.yaml`. Run
  `python gui_nav.py --help` for the command reference.
- **`doc_tools.py` doc toolkit** — wrap `gui_nav screenshot`, crop /
  annotate / arrow / label images, and render Mermaid diagrams to PNG.
  Run `python doc_tools.py --help` for the command reference.

If your task does not touch GUIs or screenshots, ignore everything in
this section.
