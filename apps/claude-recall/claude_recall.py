"""Semantic recall over Claude Code conversation transcripts.

Three subcommands:

  archive  Copy transcripts out of Claude Code's swept directories into a
           durable store. Claude Code deletes transcripts after
           `cleanupPeriodDays`; this is the second line of defence behind
           raising that setting. Append-only: nothing is ever removed here.

  index    Build the retrieval index from the archive — strip tool traffic,
           chunk, embed (dense) and tokenize (BM25).

  query    Dense + BM25 retrieval, fused by reciprocal rank, reranked by a
           cross-encoder, aggregated to sessions.

Transcript layout, per store:

    <store>/<encoded-cwd>/<sessionId>.jsonl              the conversation
    <store>/<encoded-cwd>/<sessionId>/subagents/*.jsonl  delegated work

Only the top-level file is a conversation. Subagent transcripts are ~90% of
the files and carry almost none of the "as we discussed" signal, so they are
archived but not indexed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zstandard
from dataclasses import dataclass, asdict
from pathlib import Path

# Both stores: Claude Code wrote to ~/.claude/projects before
# CLAUDE_CONFIG_DIR was set, and to $CLAUDE_CONFIG_DIR/projects after. The
# legacy store is frozen — which is the only reason anything older than the
# cleanup window still exists.
STORES = {
    "config": Path(
        os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".config" / "claude")
    )
    / "projects",
    "legacy": Path.home() / ".claude" / "projects",
}

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "claude-recall"
ARCHIVE = DATA / "archive"
INDEX = DATA / "index"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

# bge is asymmetric: the query carries an instruction prefix, the document
# does not. Omitting this silently costs retrieval quality.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200

# ONNX Runtime allocates a CPU arena per intra-op thread. Left unpinned it
# peaked at 8.7 GB on a 3.1k-chunk corpus; at two threads it peaks at 460 MB
# and runs faster. Do not remove.
THREADS = int(os.environ.get("CLAUDE_RECALL_THREADS", "2"))
BATCH = int(os.environ.get("CLAUDE_RECALL_BATCH", "8"))


# ---------------------------------------------------------------- archive


def transcript_paths(store: Path):
    """Yield (project, session_id, path, is_subagent) for one store."""
    if not store.is_dir():
        return
    for project_dir in sorted(store.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            yield project_dir.name, path.stem, path, False
        for path in sorted(project_dir.glob("*/subagents/**/*.jsonl")):
            yield project_dir.name, path.parts[-3], path, True


def cmd_archive(args) -> int:
    compressor = zstandard.ZstdCompressor(level=10)
    copied = skipped = 0
    saved_bytes = 0

    for store_name, store in STORES.items():
        for project, _session, src, is_sub in transcript_paths(store):
            rel = src.relative_to(store)
            dst = ARCHIVE / store_name / rel.with_suffix(".jsonl.zst")
            src_size = src.stat().st_size

            # Transcripts are append-only while live, so size is a sound
            # change signal. A shrunken source means Claude Code rotated or
            # truncated it — keep the larger archived copy.
            meta_path = dst.with_suffix(".zst.size")
            if dst.exists() and meta_path.exists():
                if int(meta_path.read_text()) >= src_size:
                    skipped += 1
                    continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as fh, dst.open("wb") as out:
                compressor.copy_stream(fh, out)
            meta_path.write_text(str(src_size))
            copied += 1
            saved_bytes += src_size - dst.stat().st_size

    total = sum(1 for _ in ARCHIVE.rglob("*.jsonl.zst"))
    print(
        f"archived {copied} new/changed, {skipped} unchanged; "
        f"{total} transcripts in {ARCHIVE} "
        f"(saved {saved_bytes / 1e6:.0f} MB this run)"
    )
    return 0


# ------------------------------------------------------------------ index


@dataclass
class Chunk:
    store: str
    project: str
    session: str
    ts: str
    offset: int


def _message_text(message: dict) -> str:
    """Prose only — tool_use and tool_result blocks are dropped.

    Tool traffic is ~93% of transcript bytes and is noise for recall: it is
    machine output, not anything either party said.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def read_conversation(path: Path) -> tuple[str, str]:
    """Return (prose, first_timestamp) for one archived transcript."""
    dctx = zstandard.ZstdDecompressor()
    turns: list[str] = []
    first_ts = ""
    with path.open("rb") as fh, dctx.stream_reader(fh) as reader:
        for line in reader.read().decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") not in ("user", "assistant"):
                continue
            text = _message_text(record.get("message") or {}).strip()
            # System reminders are harness plumbing injected into the user
            # turn, not something the user wrote.
            if not text or text.startswith("<system-reminder>"):
                continue
            first_ts = first_ts or record.get("timestamp", "")
            turns.append(f"{record['type'].upper()}: {text}")
    return "\n\n".join(turns), first_ts


def cmd_index(args) -> int:
    import numpy as np
    from fastembed import TextEmbedding

    chunks: list[Chunk] = []
    texts: list[str] = []

    for store_name in sorted(STORES):
        root = ARCHIVE / store_name
        if not root.is_dir():
            continue
        # Top-level only: <project>/<sessionId>.jsonl.zst
        for path in sorted(root.glob("*/*.jsonl.zst")):
            prose, ts = read_conversation(path)
            if len(prose) < 200:
                continue
            for offset in range(0, len(prose), CHUNK_CHARS - CHUNK_OVERLAP):
                texts.append(prose[offset : offset + CHUNK_CHARS])
                chunks.append(
                    Chunk(
                        store=store_name,
                        project=path.parent.name,
                        session=path.name.removesuffix(".jsonl.zst"),
                        ts=ts,
                        offset=offset,
                    )
                )

    sessions = {(c.store, c.session) for c in chunks}
    if not chunks:
        print("nothing to index — run `claude-recall archive` first", file=sys.stderr)
        return 1
    print(f"{len(sessions)} sessions -> {len(chunks)} chunks; embedding…", flush=True)

    embedder = TextEmbedding(EMBED_MODEL, threads=THREADS)
    vectors = np.asarray(
        list(embedder.embed(texts, batch_size=BATCH)), dtype=np.float32
    )
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    INDEX.mkdir(parents=True, exist_ok=True)
    np.save(INDEX / "vectors.npy", vectors)
    (INDEX / "chunks.jsonl").write_text(
        "".join(
            json.dumps({**asdict(c), "text": t}) + "\n" for c, t in zip(chunks, texts)
        )
    )
    print(
        f"indexed {len(chunks)} chunks "
        f"({vectors.nbytes / 1e6:.1f} MB) -> {INDEX}"
    )
    return 0


# ------------------------------------------------------------------ query


def load_index():
    import numpy as np

    vectors = np.load(INDEX / "vectors.npy", mmap_mode="r")
    records = [
        json.loads(line) for line in (INDEX / "chunks.jsonl").read_text().splitlines()
    ]
    return vectors, records


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Fuse ranked ID lists. Rank-based, so incommensurable scores (cosine
    vs BM25) combine without normalisation."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
    return sorted(scores, key=lambda d: -scores[d])


def cmd_query(args) -> int:
    import numpy as np
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from rank_bm25 import BM25Okapi

    vectors, records = load_index()
    question = " ".join(args.query)

    embedder = TextEmbedding(EMBED_MODEL, threads=THREADS)
    query_vector = np.asarray(
        next(iter(embedder.embed([QUERY_PREFIX + question]))), dtype=np.float32
    )
    query_vector /= np.linalg.norm(query_vector)
    dense = list(np.argsort(-(vectors @ query_vector))[: args.candidates])

    bm25 = BM25Okapi([r["text"].lower().split() for r in records])
    lexical = list(
        np.argsort(-bm25.get_scores(question.lower().split()))[: args.candidates]
    )

    fused = reciprocal_rank_fusion([dense, lexical])[: args.candidates]

    # The bi-encoder scores query and document independently, which leaves
    # them bunched and uncalibrated. The cross-encoder attends over the pair,
    # so its scores separate — and are meaningful enough to threshold on.
    reranker = TextCrossEncoder(RERANK_MODEL, threads=THREADS)
    relevance = list(reranker.rerank(question, [records[i]["text"] for i in fused]))
    ranked = sorted(zip(fused, relevance), key=lambda pair: -pair[1])

    # The session asking the question is itself in the index, and it is
    # talking about whatever was just asked — so it outranks the older
    # session actually being recalled. Drop it unless asked not to.
    excluded = set(args.exclude)
    if not args.include_current and (current := os.environ.get("CLAUDE_CODE_SESSION_ID")):
        excluded.add(current)

    best: dict[tuple, tuple[float, dict]] = {}
    for doc_id, score in ranked:
        record = records[doc_id]
        if record["session"] in excluded:
            continue
        key = (record["store"], record["session"])
        if key not in best:
            best[key] = (score, record)

    hits = [(s, r) for (s, r) in best.values() if s >= args.floor][: args.limit]
    if not hits:
        top = max((s for s, _ in best.values()), default=float("-inf"))
        print(
            f"No session in your history matches that "
            f"(best relevance {top:.1f}, floor {args.floor:.1f}).",
            file=sys.stderr,
        )
        return 1

    for score, record in hits:
        path = (
            ARCHIVE
            / record["store"]
            / record["project"]
            / f"{record['session']}.jsonl.zst"
        )
        # Claude Code's directory encoding replaces '/' with '-' and is
        # therefore lossy — '-home-vringar-nixos-setup' could decode to
        # either /home/vringar/nixos-setup or /home/vringar/nixos/setup.
        # Print it as stored rather than guessing wrong.
        print(f"score    {score:.2f}")
        print(f"session  {record['session']}")
        print(f"project  {record['project']}")
        print(f"date     {record['ts'][:10]}")
        print(f"archive  {path}")
        excerpt = " ".join(record["text"].split())[:400]
        print(f"excerpt  {excerpt}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="claude-recall", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("archive", help="copy transcripts into the durable store")
    sub.add_parser("index", help="rebuild the retrieval index from the archive")

    query = sub.add_parser("query", help="find sessions by topic")
    query.add_argument("query", nargs="+")
    query.add_argument("-n", "--limit", type=int, default=3)
    query.add_argument("--candidates", type=int, default=50)
    query.add_argument(
        "--floor",
        type=float,
        default=0.0,
        help="minimum cross-encoder relevance; below this, report no match",
    )
    query.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="SESSION",
        help="session id to omit from results (repeatable)",
    )
    query.add_argument(
        "--include-current",
        action="store_true",
        help="do not auto-exclude $CLAUDE_CODE_SESSION_ID",
    )

    args = parser.parse_args()
    return {"archive": cmd_archive, "index": cmd_index, "query": cmd_query}[args.cmd](
        args
    )


if __name__ == "__main__":
    sys.exit(main())
