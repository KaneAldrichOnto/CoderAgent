# Tool-smoke test prompts

These are minimal `prompt.md` files for the CoderAgent loop. Each one
exercises a small slice of `doc_tools.py` or `gui_nav.py` and creates
`.agent_done` when finished, so they exit on their own.

## How to run one

From the repo root:

```powershell
# Pick a test, e.g. doc_tools image basics
Copy-Item Tests\test_doc_tools_image.md prompt.md -Force

# Make sure deps are installed (Pillow for image tests, mmdc for diagrams,
# pywinauto for gui_nav tests). See AGENTS.md for first-time setup.
python setup.py

# Run the loop. The test creates .agent_done and exits.
python agent.py
```

After the run, inspect the artifacts in the working directory and the
`commit_log.md` to confirm the model actually used the tools (vs.
hallucinating output).

## Run them all

```powershell
python Tests/run_all_tests.py                 # run every test_*.md
python Tests/run_all_tests.py --filter image  # filter by filename substring
python Tests/run_all_tests.py --stop-on-fail  # abort on first failure
python Tests/run_all_tests.py -- --once       # forward flags to agent.py
```

Each test runs in its own scratch directory under
`Tests/_runs/<test_name>/<timestamp>/`, so artifacts and commits from
one test never bleed into another. A test PASSes only if `agent.py`
exits 0 *and* `.agent_done` is present at the end. The runner keeps
the last 5 run dirs per test by default (`--keep-runs N` to change).

## Index

| File | Scope | Extra deps |
| --- | --- | --- |
| [test_doc_tools_image.md](test_doc_tools_image.md) | crop / annotate-rect / annotate-arrow / annotate-label / view-image | Pillow |
| [test_doc_tools_diagram.md](test_doc_tools_diagram.md) | render-diagram (Mermaid → PNG) | `npm i -g @mermaid-js/mermaid-cli` |
| [test_gui_nav_sample_gui.md](test_gui_nav_sample_gui.md) | gui_nav serve / tree / set-text / click / screenshot against [sample_gui.py](sample_gui.py) (Tkinter) | pywinauto, Pillow, Windows |

## Notes

- Each prompt assumes the agent's `work_dir` is the test scratch dir
  (the runner sets this up automatically). Clean up with
  `Remove-Item -Recurse Tests\_runs` for a fresh slate.
- The GUI test launches its own Tkinter window
  ([sample_gui.py](sample_gui.py)) — it does not touch Notepad or any
  other installed app. It self-skips on non-Windows.
- The runner self-elevates via UAC at startup (a new admin console
  pops up with `cmd /k` so output stays visible). All `agent.py`
  invocations then run synchronously inside that elevated process
  with `--no-elevate`, so you get admin permissions everywhere AND
  the runner can actually wait on each test. Pass `--no-elevate` to
  the runner to skip elevation (some GUI tests will then hit
  permission-denied errors).
- These tests are intentionally small. They confirm the model *can*
  invoke each tool — they do not exhaustively cover every flag.
