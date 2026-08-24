---
name: recall
description: Use when something was worked out in an earlier Claude Code session and the answer should be recovered rather than re-derived — "as we discussed", "another session", "we figured this out", "did we ever". Semantic search over archived conversation transcripts via the claude-recall CLI. Also covers reading a transcript once a session is found.
---

# /recall — search previous conversations

`claude-recall` is a CLI on `PATH`, not part of the harness. Three subcommands, run in order:

```bash
claude-recall archive   # copy transcripts into the durable store (append-only, fast)
claude-recall index     # rebuild the retrieval index (SLOW — see below)
claude-recall query "topic words" -n 3
```

Retrieval is BM25 plus dense embeddings fused by reciprocal rank, then reranked by a cross-encoder. Results are whole sessions with a score and an excerpt, not passages.

## Run index in the background

`index` re-embeds the corpus and takes many minutes — it will blow past a foreground command timeout. Run it in the background and keep working.

`archive` prints `archived N new/changed, M unchanged`. **If N is 0, skip `index`** — nothing changed, the existing index is current.

## When the index is stale or still rebuilding, grep the archive

The archive is zstd-compressed JSONL, one file per session, so it can be searched directly with no index at all. This is often the fastest path when you already know a distinctive term:

```bash
A=~/.local/share/claude-recall/archive
for f in $(find "$A" -name "*.jsonl.zst"); do
  n=$(zstdcat "$f" 2>/dev/null | grep -ci "your-term" || true)
  [ "${n:-0}" -gt 0 ] && echo "$n  $f"
done | sort -rn | head
```

## Reading a session once you have a hit

`query` gives you an archive path. The excerpt is rarely enough — extract the actual conversation, dropping tool traffic:

```bash
zstdcat "$ARCHIVE_PATH" | python3 -c "
import sys, json
for line in sys.stdin:
    try: d = json.loads(line)
    except: continue
    m = d.get('message') or {}
    role = m.get('role') or d.get('type')
    c = m.get('content')
    if isinstance(c, str): text = c
    elif isinstance(c, list):
        text = ' '.join(b.get('text','') for b in c
                        if isinstance(b, dict) and b.get('type') == 'text')
    else: text = ''
    if text.strip() and role in ('user','assistant'):
        print(f'--- {role} ---'); print(text.strip()); print()
"
```

Check the session's date before trusting it — an old session may describe a version, pin, or API that has since moved. Verify anything load-bearing against the current tree rather than quoting it forward.

## Flags worth knowing

| Flag | Effect |
|---|---|
| `-n LIMIT` | number of sessions returned |
| `--floor FLOOR` | minimum cross-encoder relevance; below it, reports no match rather than a weak guess |
| `--exclude SESSION` | omit a session id (repeatable) |
| `--include-current` | do not auto-exclude `$CLAUDE_CODE_SESSION_ID` |

## Corpus shape

Two transcript stores are archived, because `CLAUDE_CONFIG_DIR` moved at some point:

- `~/.config/claude/projects/` — active
- `~/.claude/projects/` — legacy, frozen

Per project directory, `<encoded-cwd>/<sessionId>.jsonl` is the conversation and `<encoded-cwd>/<sessionId>/subagents/*.jsonl` is delegated work. **Subagent transcripts are archived but never indexed** — they are ~90% of the files and carry almost none of the "as we discussed" signal. If a query finds nothing, that is not why; they would not have helped.
