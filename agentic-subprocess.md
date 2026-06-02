# /agentic-subprocess — Camunda 8 AI Agent Ad-Hoc Sub-Processes

Use when building or editing an AI Agent AHSP — a `bpmn:adHocSubProcess` carrying the `io.camunda.connectors.agenticai.aiagent.jobworker.v1` (job-worker) or `...outbound.v*` (outbound-connector) element template. Covers prompt structure, output format, tool error handling, and the `toolCallResult` lifecycle. Camunda 8.8+ assumed.

The templated AHSP is the **agent**. The tool-shaped child elements inside it (service tasks, script tasks with `fromAi(...)` inputs, sub-processes) are the **tools** the model can call.

## System prompt — what belongs and what doesn't

The system prompt encodes **strategy** and **output contract**. It does NOT enumerate tools.

- **DO** write: when to call X before Y, what to do when a tool returns nothing, how to escalate, when to stop, what shape the response takes, what triggers the Out-of-scope signal.
- **DO NOT** write: a "Tool usage" / "Available tools" / "Tools you have" list. The runtime injects each tool's name + description from its element-template configuration (`<bpmn:documentation>`, the task name, the per-tool description on its connector template). Restating them creates two copies that drift the moment someone adds a new tool to the AHSP and forgets the prompt — and Stefan has gotten review feedback on exactly that more than once.
- If a tool's purpose isn't obvious from its runtime-injected description, **improve the tool's `<bpmn:documentation>`** — don't paper over it in the prompt.
- Per-tool nudges that apply only to *this agent's* use of *one* tool ("prefer Glean before GitHub on the first iteration") are strategy, not description — those are fine in the prompt.

Verification trail in the connector source (camunda/connectors, `connectors/agentic-ai`): tool definitions originate from `AdHocToolsSchemaResolverImpl`, are wrapped by `AgentToolsResolverImpl`, and serialised to provider-native tool specifications by `ToolSpecificationConverterImpl`. Descriptions ride along; the LLM sees them on every request.

## Output format — pick one of two established patterns

### Pattern A — Provider-native JSON mode + schema (preferred for new agents)

On the AI Agent template:

- **Response format** = `json`
- **Response JSON schema** = a FEEL Map literal modelling JSON Schema
- **Response JSON schema name** = a short name the model sees as a type label (default `"Response"`)

For Bedrock+Anthropic and OpenAI, langchain4j translates this into a forced tool-call under the hood — the model is structurally prevented from emitting prose preambles. Provider-side rejects malformed args. On parse failure the connector throws `ConnectorException(ERROR_CODE_FAILED_TO_PARSE_RESPONSE_CONTENT)` → job failure → incident with the configured retries. **No silent `responseJson = null`.**

The connector does NOT post-hoc schema-validate. Enforcement lives at the provider.

### Pattern B — Free-text XML + FEEL substring extraction (established Ticket Genie pattern)

The established pattern across `Camunda version agent.bpmn`, `Environment type agent.bpmn`, `Storage backend agent.bpmn`, `Externally hosted service agent.bpmn`, `Deployment method agent.bpmn`, `Category agent.bpmn`, `SaaS extraction agent.bpmn`. Use when consistency with these agents matters, or when targeting a provider without good JSON-mode support.

- **Response format** = `text`
- **Parse text as JSON** = `=false`
- Prompt instructs the model: *"Your response **must be only** the following XML template — no additional text before or after"*, then shows a literal template with named children.
- Downstream `bpmn:scriptTask` extracts each tag via FEEL:

```feel
={
  value: string(substring before(substring after(agent.responseText, "<value>"), "</value>")),
  confidence: string(substring before(substring after(agent.responseText, "<confidence>"), "</confidence>")),
  error: substring before(substring after(agent.responseText, "<error>"), "</error>"),
  explanation: substring before(substring after(agent.responseText, "<explanation>"), "</explanation>"),
  modelCalls: agent.context.metrics.modelCalls,
  tokenInput: agent.context.metrics.tokenUsage.inputTokenCount,
  tokenOutput: agent.context.metrics.tokenUsage.outputTokenCount
}
```

For array-valued fields, ask the model to emit a JSON array inside the tag and parse with `from json(...)`. Guard against empty extracts:

```feel
sources: if substring before(substring after(agent.responseText, "<sources>"), "</sources>") != ""
         then from json(substring before(substring after(agent.responseText, "<sources>"), "</sources>"))
         else [],
```

### Why not `text` + `Parse text as JSON = true`?

The connector tries `JSON.parse` on the whole response and **silently sets `responseJson = null` on failure** (`AgentResponseHandlerImpl.handleTextResponseFormat`, the `catch (Exception e) { LOGGER.warn(...); }` branch). Models that emit a friendly preamble before the JSON object — common with Claude — defeat this mode and the failure is invisible to downstream FEEL. Either go JSON-mode (Pattern A) or commit to XML (Pattern B). Don't mix.

## Tool error handling

See the project CLAUDE.md "Agentic tool error handling" section — that's the source of truth for the `errorExpression` + `TOOL_HTTP_ERROR` boundary event + `ioMapping` promotion. Do not duplicate it here.

In one sentence: every HTTP tool task inside an AHSP needs the canonical `errorExpression` that converts 4xx/5xx into `bpmnError("TOOL_HTTP_ERROR", ..., {toolCallResult: ...})`, AND a matching interrupting error boundary event with an `ioMapping` output `=toolCallResult -> toolCallResult`. Connection failures and other non-HTTP errors must still raise incidents — they're infrastructure problems the agent cannot react to.

## toolCallResult lifecycle

Each tool element's job emits a value into the AHSP's `outputCollection` (typically `toolCallResults`) via its `outputElement` mapping. When the AI Agent's next iteration fires, the connector sees the new entries in `toolCallResults` and feeds them back into the conversation as tool-result messages keyed by the original tool-call id. The agent then reasons over the results and either calls more tools or emits the final response.

The boundary-event pattern in CLAUDE.md hooks into this exact channel: a tool that fails HTTP returns a `{statusCode, error}` shape via the boundary's `ioMapping`, which lands in `toolCallResults` just like a successful result would. From the model's perspective there's no distinction — it just sees that the tool reported an error and can adapt.

## Out-of-scope signal

For agents that should refuse to fabricate a low-confidence answer, define an `intermediateThrowEvent` inside the AHSP named e.g. "Out of scope". Attach an interrupting boundary event of the same throw signature to the AHSP itself, routing to a "skip" script that synthesises a deterministic empty/Low-confidence response variable. The system prompt explicitly tells the model to invoke this signal when no diagnosis is possible. This decouples "no answer" from "bad parse" — bad parses incident, no-answer flows cleanly.

Reference: `Intent resolution agent.bpmn`'s `Event_OutOfScopeCatch` + `Activity_BuildSkipResponse`.

## Confidence semantics & downstream skip

Convention: a parsed `confidence` of `"Low"` (or absent/empty, defaulting to `"Low"` in the extraction FEEL) signals "no actionable suggestion". Downstream consumers (response-draft agent, comment renderer) check `if confidence != "Low" then ... else null` and skip emitting a suggestion. This makes the no-answer path safe-by-default — a parse failure degrades to "no comment" rather than "high-confidence guess from a malformed response".

The draft agent's matching contract: if its input findings indicate `confidence == "Low"` or null suggestion, it emits exactly the literal string `NO_DRAFT` instead of a customer-facing reply, and the comment-builder strips that case.

## Memory storage type & context window

The template offers three storage modes: `in-process` (default — agent context inlined into the BPMN variable), `camunda-document` (offloaded to document storage; useful when context grows past a few hundred KB), and `custom`.

- Default to `in-process` until you see Camunda variable-size warnings in Operate or hit the 4 MB job-payload ceiling.
- Default **Context window size** is 20 most-recent messages. For agents that call many tools per iteration, bump to 30–40 to avoid the model losing the chain of reasoning. Lower for cost-bounded agents that don't need long memory.

## Element template — using et-cli (not hand-XML)

The AHSP itself, the AI-agent template fields, and any tool-shape element with `zeebe:modelerTemplate` set must be edited via `element-templates-cli`. Hand-encoding XML for these fields will produce schema-broken element templates and silent-failure surprises. The associated `bpmn:scriptTask` parsers and the wiring elements (boundary events, sequence flows, sub-processes that don't carry a template) are plain BPMN and can be edited directly.

Pattern for setting a large field (system prompt, FEEL expression, schema literal):

```bash
element-templates-cli set \
  --diagram <path-to.bpmn> \
  --template-id io.camunda.connectors.agenticai.aiagent.jobworker.v1 \
  --element <ahsp-id> \
  --target "System prompt" \
  --value "$(cat /tmp/prompt.feel)" \
  --output <path-to.bpmn>
```

The `$(cat ...)` form preserves multi-line content, `\"`-escaped quotes, the leading `=` FEEL prefix, and backticks without shell quoting hell. Caveat: trailing newlines are stripped — never material for prompts or FEEL.

See `/element-templates` for the full CLI reference.

## Verification checklist before deploying

- `npx bpmnlint <file.bpmn>` is clean (the repo disables `label-required` but the rest applies).
- `element-templates-cli query --element <id>` shows your fields actually set.
- Smoke test on a known ticket. If using Pattern B, eyeball the raw `agent.responseText` (via `/v2/variables/<key>` fetched untruncated) and confirm every tag is present and the FEEL extraction is producing the expected values. If using Pattern A, confirm `agent.responseJson` is populated and the schema fields landed.
- If a smoke run silently produces `confidence = "Low"` when you expected `High`, your XML tags didn't match (Pattern B) or your provider didn't accept the JSON-mode contract (Pattern A) — inspect `agent.responseText` before tweaking the prompt.

## Worked example pointers

- **Pattern B (XML)** — `process-applications/ticket-genie/src/main/resources/camunda/Camunda version agent.bpmn`. Minimal, single-shot, single XML wrapper.
- **Pattern B with array field** — `SaaS extraction agent.bpmn`, where `<value>` carries a JSON array parsed with `from json(...)`.
- **Two-stage diagnosis + draft + out-of-scope** — `Intent resolution agent.bpmn`. The `Activity_GenerateResponseAgent` AHSP, `Activity_ParseDiscoveryFindings` script extractor, `Event_OutOfScopeCatch` boundary, and the downstream `Activity_DraftResponse` AHSP form the canonical multi-stage pattern.
- **HTTP tool error pattern** — every REST tool task inside `Intent resolution agent.bpmn`'s `Activity_GenerateResponseAgent`. Match this exactly when adding new HTTP tools.
