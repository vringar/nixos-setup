---
name: agentic-subprocess
description: Local conventions for editing a Camunda 8 AI Agent ad-hoc sub-process (AHSP). Layers on top of process-os' canonical reference — covers the c8ctl editing pattern, a pre-deploy verification checklist, and pointers to canonical worked examples in camunda/eaat.
---

# /agentic-subprocess — Local AHSP conventions

For everything operational — system prompt content, output format (Pattern A vs B), tool error handling, `toolCallResult` lifecycle, out-of-scope signal, confidence semantics, memory storage and context window — read the canonical reference: process-os' `lib/camunda-dev-guide.md`, "Agentic BPMN (AI Agents)" section. The `/new-agent-process` skill points there too.

This skill only adds the local-only conventions on top of that reference: how to *edit* AHSP fields without hand-XML, a pre-deploy verification checklist, and pointers to canonical worked examples in `camunda/eaat`.

## Editing AHSP element-template fields — use `c8ctl element-template`, not hand-XML

The AHSP itself, the AI-agent template fields, and any tool-shape element with `zeebe:modelerTemplate` set must be edited via `c8ctl element-template`. Hand-encoding XML for these fields produces schema-broken element templates and silent-failure surprises. The associated `bpmn:scriptTask` parsers and the wiring elements (boundary events, sequence flows, sub-processes that don't carry a template) are plain BPMN and can be edited directly.

Pattern for the *first* configuration of an AHSP or tool element (attaching the template):

```bash
c8ctl element-template apply \
  io.camunda.connectors.agenticai.aiagent.jobworker.v1 \
  <ahsp-id> <path-to.bpmn> \
  --set "systemPrompt=$(cat /tmp/prompt.feel)" \
  --in-place
```

The `$(cat ...)` form preserves multi-line content, `\"`-escaped quotes, the leading `=` FEEL prefix, and backticks without shell-quoting hell. Caveat: trailing newlines are stripped — never material for prompts or FEEL.

**Only run `apply` once per element.** It resets every `Hidden`-typed template property to its template default on every call — including hand-customized content merged into a template-owned extension attribute, like a custom conditional in an AHSP's `outputElement`. Re-running `apply` on an already-templated element will silently clobber that. For every change after the first `apply`, use `edit` instead:

```bash
c8ctl element-template edit \
  <ahsp-id> <path-to.bpmn> \
  --set "systemPrompt=$(cat /tmp/prompt.feel)" \
  --in-place
```

`edit` takes no `<template>` argument — it reads the template id/version off the element itself and only writes the properties you `--set`, never touching other template-owned content. It can only change bindings that already have a materialized value; use `apply --set` once to materialize a newly-gated field, then go back to `edit`.

**Binding names** (the keys in `--set name=value`) are not the same as field labels shown in Modeler. Run `c8ctl element-template get-properties <template-id> --detailed` to discover them before setting.

See `/element-templates` for the full CLI reference.

## Verification checklist before deploying

- `npx bpmnlint <file.bpmn>` is clean (the work repo disables `label-required` but the rest applies).
- Re-open the diagram and check the fields you set landed and no *other property values* changed — catches typo-binding-name silent fails and (if `apply` was mistakenly re-run) clobbered hand-authored content. Both `apply` and `edit` round-trip through bpmn-js's `saveXML`, so BPMNDI shape/edge reordering in the diff is expected cosmetic noise, not a sign of a problem — don't chase it.
- Smoke test on a known ticket. If using Pattern B, eyeball the raw `agent.responseText` (via `/v2/variables/<key>` fetched untruncated) and confirm every tag is present and the FEEL extraction produces the expected values. If using Pattern A, confirm `agent.responseJson` is populated and the schema fields landed.
- If a smoke run silently produces `confidence = "Low"` when you expected `"High"`, your XML tags didn't match (Pattern B) or your provider didn't accept the JSON-mode contract (Pattern A) — inspect `agent.responseText` before tweaking the prompt.

## Worked-example pointers in `camunda/eaat`

- **Pattern B (XML), minimal single-shot** — `process-applications/ticket-genie/src/main/resources/camunda/Camunda version agent.bpmn`. Single XML wrapper.
- **Pattern B with array field** — `process-applications/ticket-genie/src/main/resources/camunda/SaaS extraction agent.bpmn`, where `<value>` carries a JSON array parsed with `from json(...)`.
- **Two-stage diagnosis + draft + out-of-scope** — `process-applications/ticket-genie/src/main/resources/camunda/Intent resolution agent.bpmn`. The `Activity_GenerateResponseAgent` AHSP, `Activity_ParseDiscoveryFindings` script extractor, `Event_OutOfScopeCatch` boundary, and the downstream `Activity_DraftResponse` AHSP form the canonical multi-stage pattern.
- **HTTP tool error pattern** — every REST tool task inside `Intent resolution agent.bpmn`'s `Activity_GenerateResponseAgent`. Match this exactly when adding new HTTP tools.
