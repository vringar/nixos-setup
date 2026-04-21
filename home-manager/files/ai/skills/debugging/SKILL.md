---
name: debugging
description: Use when diagnosing a hard or surprising bug — enforces epistemic rigor over gut-feel fixing. Claude should invoke this proactively when thrashing is detected.
---

# /debugging - Epistemically Rigorous Debugging

The failure mode in hard bugs is treating debugging as trial-and-error rather than as science.
Every fix attempt without a hypothesis is an uncontrolled experiment — it generates noise, not knowledge.

## When Claude Should Invoke This (Proactively)

Claude must step back and invoke this process when any of these are true:

- **Three or more failed CI runs** without a confirmed hypothesis change between them
- Either party is suggesting a new change without having confirmed or denied the previous theory
- The proposed next action is "let's try X" with no stated prediction of what X would prove

When this happens, Claude should stop and say: **"We're thrashing. What is our current hypothesis, and what would falsify it?"**

## Epistemic Labelling

Every statement made during debugging must be explicitly tagged with its epistemic status. This prevents unverified claims from quietly hardening into assumed facts.

| Tag | Meaning | Example |
|---|---|---|
| `[CLAIM]` | Asserted but not yet verified — must be tested | `[CLAIM] The timeout is caused by missing connection pool config` |
| `[OBSERVED]` | Direct result of a controlled experiment or inspection | `[OBSERVED] Adding LOG=debug shows the pool is never initialized` |
| `[SOURCE: <ref>]` | Backed by authoritative external knowledge | `[SOURCE: https://docs.example.com/pool] Default pool size is 0 when unconfigured` |

**Rules:**
- Never let a `[CLAIM]` drive a fix decision. A claim must become `[OBSERVED]` or `[SOURCE]` first.
- When referencing code in a codebase, use a **GitHub permalink** (commit SHA, not branch) so the reference stays valid as the code evolves. Example: `[SOURCE: https://github.com/org/repo/blob/a3f8c21/src/pool.ts#L47]`
- AI-generated statements about how a system works are `[CLAIM]` until verified — even if stated confidently.

## The Process

### 1. State the symptom precisely

Before touching anything:
- What is the exact observed behavior? (error message, wrong output, hang — be literal)
- What is the expected behavior?
- Under what exact conditions does it occur? (OS, env, inputs, timing)

Tag initial observations as `[OBSERVED]`. Everything else at this stage is `[CLAIM]`.

Vague problem → vague solution. Precision here saves everything downstream.

### 2. Form falsifiable hypotheses

Write each hypothesis as a testable claim with an explicit prediction:

> `[CLAIM]` I believe [cause] because [evidence], which predicts that [observable consequence].

A hypothesis is only useful if it can be *wrong*. "Maybe it's something with the environment" is not a hypothesis. "The failure is caused by missing env var X, which means adding it should make the test pass" is.

Design experiments to **disprove**, not confirm. Confirmation bias makes every vague theory look right.

### 3. Build a minimal reproducer — especially for CI-only failures

If the failure only happens in CI, **invest heavily in reproducing it locally** before attempting any fix. Fixing blind in CI is expensive: each attempt costs a full pipeline run and you can't inspect state.

A minimal reproducer:
- Eliminates every variable not essential to the failure
- Runs fast and locally
- Makes the failure deterministic

For CI-only failures: replicate the CI environment (env vars, user, filesystem state, Docker image). Check what differs between local and CI — that delta is your hypothesis space.

### 4. Run one experiment at a time

Each experiment tests exactly one thing. Make the change, observe the result, record what you learned, restore if inconclusive.

Never stack changes between observations. If two things change and the test passes, you know nothing about which one fixed it — or whether it was a fluke.

### 5. Interpret the result — don't just move on

After each run, update the epistemic status of your hypothesis:
- **Prediction held:** `[CLAIM]` → `[OBSERVED: confirmed]`. Now you understand the cause.
- **Prediction failed:** `[CLAIM]` → `[OBSERVED: refuted]`. This is valuable — update your model and form a new hypothesis.
- **Ambiguous:** The experiment wasn't controlled enough. Tighten it before running again.

A failed experiment that updates your understanding is progress. A failed experiment that produces "hm, weird" is noise.

### 6. Fix the root cause, not the symptom

Once the root cause is confirmed (`[OBSERVED]`), the fix is usually obvious. If it still feels like a guess, you haven't confirmed the hypothesis — go back to step 2.

Symptom fixes leave the root cause in place to resurface later, often in a harder-to-diagnose form.

## Debugging Report

Every non-trivial debugging session should produce a report. This is the artifact that captures what was learned — not just what was changed.

```markdown
## Debugging Report: <short title>

### Problem
<Exact symptom. Reproduction conditions. What was expected vs. observed.>

### Hypotheses

#### H1: <name>
- **Statement:** [CLAIM] ...
- **Prediction:** If true, then ...
- **Experiment:** <what was done to test it>
- **Result:** [OBSERVED: confirmed | refuted | ambiguous] ...
- **Conclusion:** <what this rules in or out>

#### H2: <name>
...

### Root Cause
[OBSERVED] <confirmed cause, with evidence trail>
[SOURCE: <permalink if applicable>]

### Action Items
- [ ] <specific fix or follow-up>
- [ ] <any related risks uncovered>
```

**Notes on the report:**
- Refuted hypotheses are as valuable as confirmed ones — don't omit them.
- Codebase references must use GitHub permalinks (commit SHA, not branch name).
- The report is the deliverable. A fix without a report means the knowledge is lost.

## Failure Modes

| Pattern | What it looks like | Why it's harmful |
|---|---|---|
| Untagged claims | Stating "the issue is X" without evidence | Unverified beliefs drive decisions invisibly |
| Shotgun changes | Multiple things changed between runs | Can't attribute the fix; root cause unknown |
| Unfalsifiable hypothesis | "It might be an env issue" | No experiment can test it; endless thrashing |
| Skipping the reproducer | Pushing to CI to test a theory | Each run is slow and state is hard to inspect |
| Confirmation bias | Only running tests that might support the fix | Misses disconfirming evidence |
| Symptom fixing | The error disappears but the cause remains | Resurfaces later, harder to find |
| Abandoned ambiguity | "That didn't work, let's try Y" | Loss of signal; you may have been close |

## Heuristics

- If a bug feels impossible, one of your assumptions is wrong. List your `[CLAIM]`s and test the most load-bearing one.
- The most recently changed code is the most likely culprit. Use `git bisect` or `jj` history to find when it broke.
- CI-only failures almost always trace to an environment difference. Map out every env var, user permission, and filesystem state that differs — that delta is the hypothesis space.
- If you've been debugging for >30 min without a confirmed hypothesis update, explain the problem out loud from scratch. Articulating often surfaces the wrong assumption.
- A minimal reproducer that takes an hour to build often saves a day of CI thrashing.
