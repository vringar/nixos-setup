---
name: element-templates
description: Use when interacting with element templates - applying, querying, or setting fields on BPMN elements via c8ctl element-template.
---

# /element-templates — c8ctl element-template

## Template source

The template argument to `apply`, `get-properties`, `info`, and `get` is one of:

- An OOTB id: `io.camunda.connectors.HttpJson.v2` (or `id@version` to pin a version)
- A local file path: `/path/to/template.json`
- An HTTPS URL: `https://example.com/template.json`

For official Camunda connector templates, use the id — c8ctl resolves and caches automatically.

## Subcommands

### apply
Apply a template to a BPMN element, optionally setting properties:
```bash
c8ctl element-template apply \
  io.camunda.connectors.HttpJson.v2 \
  Task_1 diagram.bpmn \
  --set "url=https://example.com" \
  --set "method=POST" \
  --in-place
```

Usage: `apply <template> <element-id> [<file.bpmn>] [--set name=value]... [--in-place]`. `--in-place` / `-i` modifies the file in place. Omit to print to stdout.

**`apply` re-applies the whole template on every call, including on an element that's already templated** — and it unconditionally resets every `Hidden`-typed template property to its template default, which silently clobbers any hand-authored content layered into a template-owned extension attribute the template doesn't fully control (e.g. a custom conditional merged into an AI Agent ad-hoc sub-process's `outputElement`). **Only run `apply` once per element**, to attach the template and set its initial properties. For every change after that, use `edit` (below) instead of re-running `apply` — `apply --set` is *not* safe to call repeatedly.

**Setting large single fields (agent prompts, FEEL expressions) from a file:**
```bash
c8ctl element-template apply \
  io.camunda.connectors.agenticai.aiagent.jobworker.v1 \
  Activity_1 diagram.bpmn \
  --set "systemPrompt=$(cat prompt.feel)" \
  --in-place
```

`$(...)` handles newlines, both quote styles, `$`, backticks, backslashes, and a leading `=` (FEEL prefix). Caveat: trailing newlines are stripped — almost never matters for prompts or FEEL.

**FEEL auto-prefix:** `=` is automatically prepended for `feel=required` properties. For `feel=optional`, include `=` explicitly when needed.

### edit
Update one or more property values on an element that **already has a template applied**, without re-running template application:
```bash
c8ctl element-template edit \
  Task_1 diagram.bpmn \
  --set "url=https://example.com/v2" \
  --in-place
```

Usage: `edit <element-id> [<file.bpmn>] --set name=value... [--in-place]`. No `<template>` argument — `edit` reads the template id/version off the element's own `zeebe:modelerTemplate` / `zeebe:modelerTemplateVersion` attributes.

**This is the safe way to make repeated changes.** `edit` writes only the properties you `--set`; it never touches any other template-owned content, so it can't clobber hand-authored extensions the way repeated `apply` calls can. The tradeoff: `edit` can only change bindings that already have a materialized value on the element. A property whose gating condition was never met (e.g. a conditional child field under a toggle that was never turned on) has nothing to edit yet — use `apply --set` once to materialize it, then switch back to `edit` for subsequent changes.

Both `apply` and `edit` round-trip the BPMN through bpmn-js's `saveXML`, so every call reorders `BPMNDI` shapes/edges as a side effect. That's expected cosmetic noise in the diff, not data loss or a sign something went wrong — when checking a diff, focus on the actual property/extension content, not the DI reordering.

### get-properties
Discover a template's settable properties and their binding names:
```bash
c8ctl element-template get-properties \
  io.camunda.connectors.HttpJson.v2

# Full detail (types, descriptions, patterns, dropdown choices):
c8ctl element-template get-properties io.camunda.connectors.HttpJson.v2 --detailed

# Filter to specific properties/groups (supports shell-style globs, quote to avoid expansion):
c8ctl element-template get-properties io.camunda.connectors.HttpJson.v2 --detailed 'authentication.*'
c8ctl element-template get-properties io.camunda.connectors.HttpJson.v2 --group authentication --group endpoint
```

`get-properties` reads the **template definition**, not a live element — it does not show an already-applied element's current values. **Run this before `apply --set` or `edit --set`** to find binding names; `--set` uses binding names as keys, not field labels.

### search
Search the OOTB template catalogue:
```bash
c8ctl element-template search "HTTP"
c8ctl element-template search "HTTP" --engine-version 8.7 --limit 5
```

### info
Show template metadata (id, version, applies-to, description):
```bash
c8ctl element-template info io.camunda.connectors.HttpJson.v2
```

### get
Print raw template JSON:
```bash
c8ctl element-template get io.camunda.connectors.HttpJson.v2
c8ctl element-template get io.camunda.connectors.HttpJson.v2 --no-icon   # drop large base64 icon
```

### sync
Refresh the local OOTB template cache:
```bash
c8ctl element-template sync
c8ctl element-template sync --prune   # also drop entries no longer in index
```

## Typical Workflow

1. `search` to find a template by name and note its id
2. `get-properties --detailed` (on the template id) to discover binding names up front
3. `apply` the template to the element **once** — with `--set` for the properties you already know, or with none at all
4. `edit --set name=value` (repeatable, one call per round of changes) for every change from then on — never re-run `apply` on an already-templated element
