# AGENTS.md

Conventions for developers (human or AI) modifying CoderAgent itself.
Keep this file short.

## Layout

- `agent.py` — the loop. Wraps a Copilot or Claude CLI in a long-running
  iteration loop with prompt assembly, scratchpad round-trip, commit
  tracking, internal logs, and timeouts.
- `setup.py` — config loading + dependency bootstrap. Idempotent; can be
  re-run any time.
- `doc_tools.py` — generic CLI toolkit: image crop / annotate / arrow /
  label, Mermaid → PNG, screenshot wrapper. App-agnostic.
- `gui_nav.py` — optional persistent UIA automation server for Windows.
  Target process is required via `--process` or `target_process:` in
  `CoderAgentConfig.yaml`. **No app-specific defaults.**
- `CoderAgentConfig.example.yaml` — committed template. Real config
  lives in `CoderAgentConfig.yaml` (git-ignored).
- `prompt.example.md` — committed template. Real prompts live in
  `prompt.md` (git-ignored).

## Hard rules

1. **Do not hard-code app-specific values.** Process names, file paths,
   GUI control names, and document layouts belong in user overlays —
   never in this repo.
2. **Optional dependencies stay optional.** Anything beyond the standard
   library + the CLI you actually run (`gh copilot`, `claude`, or
   `copilot`) must be guarded with `try: import ... except ImportError`
   and degrade with a clear install hint. Currently optional:
   `pywinauto`, `pillow`, `mmdc` (npm), `@github/copilot` (npm).
3. **`--help` must always exit 0**, even when optional deps are absent.
   Real subcommands print a clear "X is required, install with: ..."
   message and exit non-zero.
4. **Cross-platform.** GUI features are Windows-only and must say so
   politely on Linux/macOS. The core loop must keep working everywhere
   it works today.
5. **Don't refactor unrelated code.** If you find a tempting cleanup,
   add it to `agent_scratchpad.md` under "Future work" and move on.

## Adding a new `doc_tools.py` subcommand

1. Write a `cmd_<name>(args) -> int` function. Return 0 on success,
   non-zero on error. Print errors to `sys.stderr`.
2. If the command needs Pillow (or any optional dep), check the module
   flag (`PIL_AVAILABLE`, etc.) at the top of the function and print
   the install hint if missing. Do not raise.
3. Register a subparser in `_build_parser()` with a one-line `help=`.
   Use `set_defaults(func=cmd_<name>)`.
4. Run `python doc_tools.py --help` to verify your command appears and
   `python -m py_compile doc_tools.py` passes.

## Housekeeping wrapper contract

`agent.py:build_full_prompt()` appends a "Housekeeping" section to every
iteration's prompt. The contract with the model is:

1. Plan, then act.
2. Commit regularly.
3. Stay on task.
4. Verify screenshots / generated images via vision in the SAME turn
   (the `copilot` CLI is multimodal — open the image file directly).
5. Stop when done by creating `.agent_done`.
6. Update `agent_scratchpad.md` (Done / Next / Blockers / Future work).

If you add a new contract item, update both `agent.py` and the
"Optional capabilities" list in `prompt.example.md`.

## App overlays (downstream wrappers)

A downstream project ships its own toolkit script (e.g.
`my_app_agent.py`) that calls `gui_nav.py` and `doc_tools.py` for the
heavy lifting. The downstream project also ships its own
`agent_context.md` (or whatever it calls the rolling app-specific
context file) and references it from its own `prompt.md`. None of that
lives in CoderAgent.
