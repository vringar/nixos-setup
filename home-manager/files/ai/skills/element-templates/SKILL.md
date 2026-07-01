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
  --diagram <diagram.bpmn> \
  --element <elementId> \
  io.camunda.connectors.HttpJson.v2 \
  --set "url=https://example.com" \
  --set "method=POST" \
  --in-place
```

`--in-place` / `-i` modifies the file in place. Omit to print to stdout.

**`apply` is non-destructive** — re-running it on an element that already has the template applied preserves all existing property values. Only properties explicitly passed via `--set` are updated. Call `apply` incrementally to configure fields in multiple steps.

**Setting large single fields (agent prompts, FEEL expressions) from a file:**
```bash
c8ctl element-template apply \
  --diagram diagram.bpmn \
  --element Activity_1 \
  io.camunda.connectors.agenticai.aiagent.jobworker.v1 \
  --set "systemPrompt=$(cat prompt.feel)" \
  --in-place
```

`$(...)` handles newlines, both quote styles, `$`, backticks, backslashes, and a leading `=` (FEEL prefix). Caveat: trailing newlines are stripped — almost never matters for prompts or FEEL.

**FEEL auto-prefix:** `=` is automatically prepended for `feel=required` properties. For `feel=optional`, include `=` explicitly when needed.

### get-properties
Discover settable properties and their binding names:
```bash
c8ctl element-template get-properties \
  --diagram <diagram.bpmn> \
  --element <elementId>

# Full detail (types, descriptions, current values, dropdown choices):
c8ctl element-template get-properties --diagram <diagram.bpmn> --element <elementId> --detailed

# Filter to a specific group:
c8ctl element-template get-properties --diagram <diagram.bpmn> --element <elementId> --group <groupId>
```

**Run this before `apply --set` to find binding names.** `--set` uses binding names as keys, not field labels.

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
2. `apply` the template to the element (no `--set` on first apply is fine)
3. `get-properties --detailed` to discover binding names and current values
4. `apply --set name=value` (repeatable) to configure — safe to call multiple times
