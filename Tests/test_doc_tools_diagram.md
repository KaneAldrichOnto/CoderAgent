# Task Prompt — doc_tools Mermaid render smoke test

## Goal

Confirm `doc_tools.py render-diagram` can turn a Mermaid source file
into a PNG, and that you can verify the PNG visually in the same turn.

## Context

- Tool reference: `python doc_tools.py --help`
- `render-diagram` shells out to `mmdc` (Mermaid CLI). Install hint:
  `npm install -g @mermaid-js/mermaid-cli`.
- Artifacts dir: `Tests/_artifacts/doc_tools_diagram/`.

## Steps

1. Write `Tests/_artifacts/doc_tools_diagram/flow.mmd` containing a
   minimal flowchart, e.g.

   ```mermaid
   flowchart LR
       A[Start] --> B{Tool works?}
       B -- yes --> C[Done]
       B -- no  --> D[Debug]
   ```

2. Run
   `python doc_tools.py render-diagram Tests/_artifacts/doc_tools_diagram/flow.mmd Tests/_artifacts/doc_tools_diagram/flow.png`.
3. `view-image` the PNG to confirm non-zero dimensions.
4. Open `flow.png` in this turn (vision) and confirm you can see the
   four nodes (`Start`, `Tool works?`, `Done`, `Debug`) and the
   labelled edges. If the render is blank or wrong, fix the `.mmd` and
   re-render.
5. Create `.agent_done`.

## Rules

- If `mmdc` is not on PATH, print the install hint and stop —
  do **not** create `.agent_done`. Surface the missing dep clearly.
- Do not modify `doc_tools.py`.
- Single commit at the end:
  `test(doc_tools): mermaid render smoke run`.
