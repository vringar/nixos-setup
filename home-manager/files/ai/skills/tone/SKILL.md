---
name: tone
description: Use when drafting or reviewing writing that goes out under Stefan's name — emails, posts, replies, PR descriptions, public comments. Opt-in; preserves his voice. Do NOT apply to Claude's own chat output.
---

# /tone — Stefan's authorial voice

Opt-in skill. Fires when Stefan is the named author. Triggers:

- "draft this under my name" / "in my voice" / "as me" / "as Stefan"
- "reply to X" / "response to Y" / "answer this DM" / "draft an email to Z"
- Polishing a draft he wrote, with instructions to keep it sounding like him
- Reviewing a post/comment and asked to keep the voice

**Do not apply to:** Claude's own chat replies, internal notes, code comments, commit messages Claude writes, or anything not attributed to Stefan. The run-on-with-parens style is a feature in his name; it would be wrong as Claude's default. Claude isn't ESL — no reason to copy ESL patterns by default.

## The four rules

1. **Embrace longer sentences with embedded clauses.** Don't break a chained sentence into essay rhythm. A sentence with two relative clauses and a parenthetical aside is fine; the rhythm is the point.
2. **Tolerate commas a copy editor would cut.** Comma before "as" / "which" / "that", comma after short intro phrases, occasional comma splice. Keep them.
3. **Prefer simpler, more common vocabulary over jargon.** "Wrangle" beats "manipulate", "mess" beats "complexity", "fix" beats "remediate". Not a weakness to mask — with exec audiences and engineering peers alike, plain words land harder than ornate ones.
4. **Preserve sentence shape on revisions.** If Stefan wrote a long comma-chained sentence and asked you to polish, fix the content, not the shape. Don't restructure into something more "polished".

## Voice markers worth keeping

Sampled from the bachelor/master posts on zabka_it and the (hand-written) thesis:

- **Wry self-aware openings.** "Configuration is hard, that's why I don't want to do it."
- **Direct judgment, no softening of the verdict.** "I think both X and Y are bad decisions as they lead to complicated and subtle code."
- **Honest hedges on uncertainty.** "I am willing to blame this on…", "It seems unlikely that…", "This needs to be explored."
- **Concrete verbs over abstract ones.** "destroy the JSON by unquoting a string", "save us an `eval`", "wrangle them into a usable structure".
- **"So-called" preserves the original name** instead of renaming for elegance.
- **Topic-then-walk-through.** State the thing, then talk through it. Don't open with a polished thesis sentence.
- **"Update:" notes** that correct an earlier claim in the same piece — leave them in.

## Things Claude tends to do that break the voice

- Splits long sentences into two or three short ones for "clarity"
- Replaces commas with semicolons, em-dashes, or periods
- Substitutes Latinate vocabulary: "elaborate" for "complicated", "mitigate" for "fix", "facilitate" for "let", "endeavor" for "try", "utilize" for "use"
- Smooths "I don't want to do it" into "this requires care"
- Removes the wry/judgmental aside in the name of professionalism
- Converts inline prose lists into bulleted lists
- Adds connective tissue ("furthermore", "in addition", "moreover") between sentences that were fine sitting next to each other

## Quick before/after

**Stefan:**
> Configuration is hard, that's why I don't want to do it.

**Claude-default rewrite (don't):**
> Configuration is challenging, which is why I prefer to avoid it.

---

**Stefan:**
> Once we have achieved that we dump the resulting config as a JSON and then destroy the JSON by unquoting a string to save us an `eval` in the WebExtension.

**Claude-default rewrite (don't):**
> Once that's complete, we serialize the config to JSON. We then strip the surrounding quotes to avoid an `eval` call in the WebExtension.

---

**Stefan:**
> However, during the setup phase (before the initial snapshot) I did not want to intercept any accesses, as I wanted the execution to continue, which depends on the emulated devices being present and working.

**Claude-default rewrite (don't):**
> During the setup phase — before the initial snapshot — I needed accesses to pass through unintercepted, so that execution could continue. This required the emulated devices to remain present and functional.

## When reviewing rather than drafting

Separate two kinds of change:

- **Content:** a wrong claim, a missing point, an unclear referent, a load-bearing word that doesn't mean what he thinks it means. Apply freely.
- **Shape:** sentence length, comma density, word register. Hold off — that's the voice.

If a sentence is genuinely confusing (not just long), flag it and ask before restructuring. Length alone isn't confusion.
