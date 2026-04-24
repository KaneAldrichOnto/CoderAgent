# Task Prompt — gui_nav sample-GUI smoke test (Windows only)

## Goal

Confirm `gui_nav.py` can attach to a custom Windows GUI app, find its
controls, type into an edit box, click a button, capture a screenshot,
and visually verify the screenshot in the same turn.

The fixture is a tiny Tkinter window shipped with the repo at
`Tests/sample_gui.py` (run it once by hand with `python
Tests/sample_gui.py` if you want to see what it looks like). It has:

- an `Entry` you can type into,
- a `Submit` button that copies the entry text into a label,
- a status `Label` that starts as `ready`,
- a `Quit` button.

Window title: **`CoderAgent Sample GUI`**.

## Context

- Tool reference: `python gui_nav.py --help`
- Conventions and first-time setup: [AGENTS.md](../AGENTS.md)
- Required deps: `pip install pywinauto pillow pywin32` (Tkinter ships
  with the stdlib Python on Windows).
- Artifacts dir: `Tests/_artifacts/gui_nav_sample/` (relative to the
  repo root). Create it if missing.
- The repo root is exposed via `--dir` so you can reference
  `Tests/sample_gui.py` from the agent's working directory using the
  path the runner sets up (typically `..\..\..\Tests\sample_gui.py`
  when the cwd is `Tests/_runs/<name>/<stamp>/`). Use whatever
  absolute path resolves on this machine — don't guess.

## Steps

1. **Bail out cleanly on non-Windows.** If `platform.system() != "Windows"`,
   print `gui_nav requires Windows — skipping` and create `.agent_done`.
   Do not fail the run.
2. **Resolve paths.** Find the absolute path to `Tests/sample_gui.py`
   (search upward from cwd if needed). Find the absolute path to
   `gui_nav.py`. Use absolute paths for every command in this test —
   the agent's cwd is a scratch dir, not the repo root.
3. **Configure target.** Either pass `--process python.exe` on every
   `gui_nav.py` call OR write `target_process: python.exe` into a
   local `CoderAgentConfig.yaml` in the cwd. (Tkinter apps run inside
   the Python interpreter, so the target process is `python.exe`.)
4. **Launch the GUI in the background.**
   `Start-Process -FilePath python.exe -ArgumentList "<abs path to sample_gui.py>"`.
   Give it ~2 seconds to appear. Verify the window exists with
   `python gui_nav.py tree` — you should see `CoderAgent Sample GUI`
   in the output. Save the tree to
   `Tests/_artifacts/gui_nav_sample/tree.txt`.
5. **Type into the entry.**
   `python gui_nav.py set-text <name-of-entry> "hello agent"`. The
   correct UIA name comes from step 4's tree dump — do not invent it.
6. **Click Submit.** `python gui_nav.py click Submit`.
7. **Screenshot.**
   `python gui_nav.py screenshot --path Tests/_artifacts/gui_nav_sample/after_submit.png`.
8. **Verify with vision (in this turn).** Open `after_submit.png`
   and confirm the status label now reads `got: hello agent`. If it
   doesn't, debug (re-dump the tree, retry the set-text/click), and
   re-capture. Do not claim success on a stale or wrong screenshot.
9. **Clean shutdown.** `python gui_nav.py click Quit`. Then
   `python gui_nav.py stop` to shut down the UIA server.
10. Create `.agent_done`.

## Rules

- Do not modify `gui_nav.py` or `Tests/sample_gui.py`.
- Do not leave the Tkinter window or the gui_nav server running on
  exit (step 9 is mandatory).
- If a control lookup fails, dump the tree and adjust — don't guess
  control names.
- Single commit at the end:
  `test(gui_nav): sample GUI smoke run`.
