# Task Prompt — doc_tools image smoke test

## Goal

Confirm you can drive `doc_tools.py` end-to-end on a simple image:
generate a source PNG, crop it, annotate it three ways, and inspect
the result. Success means each step produced a file on disk and you
verified its contents in the same turn.

## Context

- Tool reference: `python doc_tools.py --help`
- Conventions: [AGENTS.md](../AGENTS.md)
- Put every artifact under `Tests/_artifacts/doc_tools_image/`
  (create the directory if missing).

## Steps

1. Create `Tests/_artifacts/doc_tools_image/source.png`: a 400x300
   solid light-grey PNG. A 3-line Python one-liner using Pillow is
   fine — you do **not** need a `doc_tools` subcommand for this seed
   step.
2. Run `python doc_tools.py view-image Tests/_artifacts/doc_tools_image/source.png`
   and confirm the printed dimensions are 400x300.
3. `crop-image` it to a 200x200 region starting at (50, 50) →
   `cropped.png`. View it to confirm 200x200.
4. `annotate-rect` on `cropped.png` → `rect.png` with a red box
   somewhere clearly inside the image bounds.
5. `annotate-arrow` on `rect.png` → `arrow.png` pointing at the box.
6. `annotate-label` on `arrow.png` → `final.png` with the text
   `tool smoke OK` near the arrow head.
7. Open `final.png` in this turn (vision) and describe in one sentence
   what you actually see — rectangle colour, arrow direction, label
   text. If the image does not match the steps, fix and retry; do not
   claim success on a blank or wrong image.
8. Create `.agent_done` and exit.

## Rules

- All artifacts must live under `Tests/_artifacts/doc_tools_image/`.
- Do not modify `doc_tools.py` itself — this test only *uses* it.
- If Pillow is missing, install it with `pip install pillow`, then
  retry. Do not silently skip steps.
- Commit once at the end with message
  `test(doc_tools): image pipeline smoke run`.
