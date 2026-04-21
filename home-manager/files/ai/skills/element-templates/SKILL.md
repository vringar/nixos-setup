---
name: element-templates
description: Use when interacting with element templates - applying, querying, or setting fields on BPMN elements via element-templates-cli.
---

# /element-templates - Element Templates CLI

## Template Source

All subcommands accept one of (exactly one required):

- `--template-path <path>` / `--tp <path>` — local element template JSON file
- `--template-id <id>` / `--ti <id>` — resolve by template ID (e.g. `io.camunda.connectors.HttpJson.v2`); looked up via Desktop Modeler paths and cached automatically
- `--template <path>` — deprecated alias for `--template-path`

Prefer `--template-id` for official Camunda connector templates — no manual download required.

## Subcommands

### apply
Apply an element template to a BPMN element:
```bash
element-templates-cli apply \
  --diagram <diagram.bpmn> \
  --template-id io.camunda.connectors.HttpJson.v2 \
  --element <elementId> \
  --output <out.bpmn>   # omit or use "-" to print to stdout
```

`apply` is the default subcommand and may be omitted for backward compat.

### query
Discover visible fields on a template applied to an element:
```bash
element-templates-cli query \
  --diagram <diagram.bpmn> \
  --template-path <template.json> \
  --element <elementId>
```
Returns JSON grouped by section. Each field includes: `type`, `value`, `choices` (for Dropdown), `feel`, `constraints`, `description`. Hidden fields (conditions not met) are excluded. Ungrouped fields appear under `"General"`. Use this before `set` to find field labels.

### set
Set field values on a template applied to an element:
```bash
element-templates-cli set \
  --diagram <diagram.bpmn> \
  --template-id io.camunda.connectors.HttpJson.v2 \
  --element <elementId> \
  --values '{"Job type": "myWorker", "Retries": "5"}' \
  --output <out.bpmn>
```

- Keys in `--values` are field labels. Use `"Section.Label"` to disambiguate across sections; duplicate labels within a section are suffixed `(N)` (e.g. `"Label (2)"`).
- Constraints (`notEmpty`, `pattern`, `minLength`, `maxLength`) are enforced before applying.
- Supports cascading condition re-evaluation — newly-visible fields can be set in the same call.

## Official Camunda Connector Templates

The canonical index of official out-of-the-box connector templates:

```
https://raw.githubusercontent.com/camunda/connectors/main/connector-templates.json
```

This JSON is an array of `{ id, name, uri, ... }`. When the user references an "official" or "out-of-the-box" connector:

1. Fetch the index above
2. Find the matching entry by `name` or `id`
3. Use its `id` with `--template-id` (preferred — CLI resolves + caches automatically)
   Or: download the `uri` locally and pass via `--template-path`

## Typical Workflow

1. Apply a template with `apply` — use `--template-id` for official connectors
2. Run `query` to see visible fields, types, and exact labels
3. Use `set` with a JSON object mapping labels to values
4. Inspect or pipe the resulting BPMN XML as needed
