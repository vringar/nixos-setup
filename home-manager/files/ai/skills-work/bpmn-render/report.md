# Spike: headless BPMN rendering for agent visual verification

## Why

A layout-porting task in `camunda/eaat` (moving a cluster of AHSP tool tasks between 7 BPMN files) turned into repeated blind iteration: `camunda-bpmn`'s `layout-rules.md` explicitly says "rendering the diagram isn't generally available to the agent" and directs agents to rely on `c8ctl bpmn lint`'s `no-overlapping-elements` warning alone. That warning reports *that* two elements overlap but not *which* elements — so each fix required reverse-engineering the collision from raw `dc:Bounds` coordinates, applying a guess, re-linting, and repeating. Several genuine placement mistakes (colliding with unrelated sibling shapes) only surfaced after multiple rounds of this. A real visual check would have caught each one on the first pass.

This spike confirms the "not available" premise in `layout-rules.md` is no longer true, or at least worth re-examining — a working, fully-headless render pipeline exists and runs cleanly in a standard sandboxed agent environment (no display, no GPU, no pre-installed browser beyond a stock Chromium binary).

## What was verified

### `bpmn-to-image` — rendering (works)

```bash
PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser npx -y bpmn-to-image \
  process-applications/ticket-genie/src/main/resources/camunda/Camunda-version-agent.bpmn:./out.png
```

Ran against a real, complex production BPMN file from `camunda/eaat` (AI Agent ad-hoc subprocess, multiple connector-templated service tasks, error boundary events). Produced both SVG (44.8K) and PNG (46.1K) output, both visually correct — confirmed by actually viewing the PNG. The rendered AHSP tool-task grid matched the intended layout exactly, at a scale where the lint tool's reported ~13px overlap (a cosmetic nudge already present in the human-authored reference design) was invisible, i.e. genuinely not a visual problem.

No system dependency beyond a Chromium/Chrome binary (`/usr/bin/chromium-browser` in this sandbox — found via `which chromium chromium-browser google-chrome google-chrome-stable`) and Node/npx (already present in any Claude Code environment). No account, no network access beyond the one-time `npx` package fetch, no pre-baked Docker image required.

**Known limitation, confirmed against real output:** connector-template icons (Slack, HTTP, AI Agent tools) render as a generic gear-and-signal glyph rather than their real icon. This is corroborated by `camunda/team-eng-ops#391`'s own PR description reporting the identical limitation. Shape/size/position are unaffected — this only matters if a human is scanning the render for "which connector is this," not for layout correctness.

### `bpmn-js-headless` — headless editing (works, different problem)

Separately evaluated for headless *editing* (not rendering — its own README is explicit that it does no graphical rendering at all, by design, to stay usable in plain Node with no DOM shim whatsoever). Confirmed:

```javascript
import Modeler from 'bpmn-js-headless/lib/Modeler';
const modeler = new Modeler();
await modeler.importXML(xml);            // loaded a real eaat BPMN file cleanly, zero warnings
modeler.get('modeling').moveShape(shape, { x: 40, y: 0 });
// connected sequence flow's waypoints were automatically recomputed — no manual waypoint math
const { xml: outXml } = await modeler.saveXML({ format: true });
```

This is the piece that would replace today's regex-over-raw-XML approach to moving shapes: bpmn-js's own modeling API handles edge rerouting correctly, which was a real source of manual-arithmetic risk today.

**Caveat, also confirmed:** `saveXML` round-trips the *entire* document through bpmn-moddle's serializer. This causes incidental unrelated-content churn on every save — observed: a curly apostrophe (`'`) elsewhere in the document flattened to a straight one (`&#39;`), plus full `BPMNDI` shape/edge reordering. This is the exact same side effect `c8ctl element-template apply` has (also bpmn-js/bpmn-moddle-based) — it is inherent to *any* moddle load→save round-trip, not a bug specific to either tool. Anyone wiring this in needs to decide: accept the cosmetic diff noise, or keep doing surgical text-level edits for small changes and reserve the modeling API for edits complex enough that correct edge-rerouting matters more than diff cleanliness.

## Prior art at Camunda (via Glean)

This is already real, working infrastructure elsewhere in the org — not a novel idea:

- **`camunda/team-eng-ops`** — a working `/visual-diff` Claude Code skill (owner: Maxim Danilov) renders before/after BPMN screenshots on every PR using exactly `bpmn-to-image` + `PUPPETEER_EXECUTABLE_PATH`. Live examples: PR #114, #118, #119, #391. Its first version hardcoded a macOS Chrome path and broke on Linux/Windows CI (PR #115 review thread) — fixed by generalizing to `PUPPETEER_EXECUTABLE_PATH` with cross-platform discovery. There's a separate, unresolved Windows-only snag (a `gh image` upload step needs a live browser session cookie unavailable while Chrome is headless) — not relevant to Linux agent sandboxes.
- **`camunda/isms`** — its own `CLAUDE.md` instructs agents to run `npx -y bpmn-to-image assets/foo.bpmn:assets/foo.svg` routinely, to keep rendered diagrams in compliance docs in sync with source BPMN.
- **`bpmn-io/bpmn-to-image`** and **`bpmn-io/bpmn-js-headless`** are both catalogued as official first-party bpmn.io repos (same org/maintainer set as `bpmn-js` itself) — not third-party or abandoned.
- **`camunda/c8ctl-plugin-diagram-renderer`** (Volker Buzek) — an existing, unrelated c8ctl plugin already solving the harder version of this problem: puppeteer-core + a *bundled* bpmn-js (no CDN dependency), rendering a *running* process instance with execution-state highlighting. Not installed in this environment (`c8ctl diagram` returns no help), but architecturally the closest template for a proper `c8ctl-plugin-render` — its cross-platform Chromium discovery and asset-bundling approach can likely be lifted wholesale for a static-file render plugin.

## Proposed integration (for whoever picks this up)

1. **Short term:** ~~ship the `SKILL.md` alongside this report as-is — a standalone `bpmn-render` skill, usable today via `npx`, no build step.~~ Done — packaged as `apps/bpmn-to-image` (nix, pinned via npins) and wired into `home-manager/ai.nix` as a work-only skill (`home-manager/files/ai/skills-work/`), so it deploys only where `my.work.enable = true` and runs via `$BPMN_TO_IMAGE_BIN` with no `npx` or first-run download.
2. **Update `camunda-bpmn`'s `references/layout-rules.md`**: the line "rely on that signal rather than a visual check, since rendering the diagram isn't generally available to the agent" is now false and should point at `bpmn-render` instead.
3. **Medium term:** fold this into a `c8ctl-plugin-render` (mirroring `c8ctl-plugin-bpmn`'s and `c8ctl-plugin-element-template`'s existing plugin structure), modeled on `camunda/c8ctl-plugin-diagram-renderer`'s bundled-bpmn-js + cross-platform-Chromium approach, so it's a first-class `c8ctl bpmn render` subcommand rather than a bare `npx` invocation a skill has to remember.
4. **Consider improving `c8ctl bpmn lint`'s `no-overlapping-elements` message** to name the overlapping partner element, not just the offending one — cheaper win than rendering, catches most collisions without needing an image at all. Worth doing independent of the above.
