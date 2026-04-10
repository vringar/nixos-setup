---
name: element-templates
description: Use when interacting with element templates - applying, querying, or setting fields on BPMN elements via element-templates-cli.
---

# /element-templates - Element Templates CLI

## Subcommands

### apply
Apply an element template to a BPMN element:
```bash
element-templates-cli apply \
  --diagram <diagram.bpmn> \
  --template <template.json> \
  --element <elementId> \
  --output <out.bpmn>   # omit to print to stdout
```

### query
Discover available fields on a template applied to an element:
```bash
element-templates-cli query \
  --diagram <diagram.bpmn> \
  --template <template.json> \
  --element <elementId>
```
Returns JSON grouped by section. Use this before `set` to find field labels.

### set
Set field values on a template applied to an element:
```bash
element-templates-cli set \
  --diagram <diagram.bpmn> \
  --template <template.json> \
  --element <elementId> \
  --values '{"Job type": "myWorker", "Retries": "5"}' \
  --output <out.bpmn>
```
Use `"Section.Label"` to disambiguate duplicate labels across sections.

## Typical Workflow

1. Apply the template to a fresh element with `apply`
2. Run `query` to see what fields are available and their exact labels
3. Use `set` with a JSON object mapping labels to values
4. Inspect or pipe the resulting BPMN XML as needed
