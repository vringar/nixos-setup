---
name: bpmn-render
description: |
  Use this skill to render a BPMN 2.0 diagram to SVG or PNG so its layout can be visually verified — after hand-editing DI coordinates, porting a layout between diagrams, or resolving a merge conflict in the diagram section.

  Use for: confirming a diagram actually looks right (not just "no overlap warnings") before calling a layout edit done; producing a before/after screenshot to attach to a PR description or review.

  Do not use for: structural/schema validation (use camunda-bpmn's `c8ctl bpmn lint` first — it's faster and catches overlap/missing-DI errors cheaply); editing the diagram (use camunda-bpmn or bpmn-js-headless for that — this skill only renders).

  **Utility skill** — pairs with camunda-bpmn. Run lint first (cheap, structural), then render (expensive, visual) only when you need to actually see the result.
---

# BPMN Render

Render a `.bpmn` file to a real image via headless Chromium, using the same rendering engine (`bpmn-js`) that Camunda Modeler itself uses. This closes the gap `camunda-bpmn`'s `layout-rules.md` used to document as a hard limitation ("rendering the diagram isn't generally available to the agent") — it now is, via `bpmn-to-image`.

## Prerequisites

`bpmn-to-image` is preinstalled and wrapped with Chromium + `PUPPETEER_EXECUTABLE_PATH` already baked in — no `npx`, no first-run download. The binary path is exported as `$BPMN_TO_IMAGE_BIN`.

## Usage

```bash
"$BPMN_TO_IMAGE_BIN" path/to/diagram.bpmn:path/to/output.png
```

- Swap `.png` for `.svg` for vector output (smaller, infinitely zoomable — prefer this unless you specifically need a raster image for e.g. a chat upload).
- Multiple `input:output` pairs can be passed in one invocation; see `"$BPMN_TO_IMAGE_BIN" --help`.

Then view the output with a normal image-reading tool (e.g. Claude Code's `Read` tool handles PNG directly; for SVG, either read it as PNG or view the raw SVG text if the shapes/positions are what matters, not the pixels).

## Workflow: verify a layout edit

1. Make the DI edit (hand-edit, `bpmn-js-headless` modeling API, or Modeler).
2. `c8ctl bpmn lint path/to/diagram.bpmn` — cheap, catches `no-overlapping-elements` and structural issues first. Fix those before rendering; no point rendering a file lint already flagged.
3. Render with the command above, into a scratch path (never commit rendered images to the repo — they're a verification artifact, not a build output).
4. Read the image. Confirm the edit looks like what was intended — lint passing does not mean it looks right, only that nothing's colliding.

## Known limitations

- **Connector template icons don't render.** A service task with an applied element template (Slack, HTTP, an AI Agent tool) renders as a generic gear-and-signal glyph, not the connector's real icon. Cosmetic only — shape, size, and position are accurate.
- **No collision-avoidance.** This only renders; it does not compute non-overlapping positions for you. Combine with careful placement (see `bpmn-js-headless`'s `modeling.moveShape`, which reroutes connected edges automatically but still won't dodge unrelated siblings on its own).
- **Real headless Chromium, not jsdom.** jsdom does not implement enough of SVG layout for bpmn-js to render correctly — this genuinely needs a browser engine, just an automated/no-display one. Cold start is ~1-2s per render; batch multiple files in one invocation when possible rather than shelling out per file.

## Prior art at Camunda

This is not a novel approach — see `report.md` alongside this skill for the internal precedent (`camunda/team-eng-ops`'s `/visual-diff`, `camunda/isms`'s `CLAUDE.md` usage) and a proposed path to a proper `c8ctl-plugin-render` (modeled on the existing `camunda/c8ctl-plugin-diagram-renderer`, which solves the same cross-platform Chromium-discovery problem for a different use case).
