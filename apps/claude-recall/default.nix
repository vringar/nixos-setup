# Semantic recall over Claude Code conversation transcripts.
#
# Deliberately serverless: `index` and `query` are short-lived processes that
# allocate, work, and exit. Resident cost is zero; peak is ~460 MB during a
# nightly index and ~380 MB for a couple of seconds per query.
#
# Model weights are not in the store. fastembed fetches them once (~150 MB
# for the embedder plus the reranker) into ~/.cache/fastembed — the same
# imperative-blob/declarative-pointer split used for the LLM service weights.
{pkgs}: let
  python = pkgs.python3.withPackages (ps: [
    ps.fastembed # ONNX Runtime — no torch, no server
    ps.numpy
    ps.rank-bm25
    ps.zstandard
  ]);
in
  pkgs.writeShellScriptBin "claude-recall" ''
    exec ${python}/bin/python3 ${./claude_recall.py} "$@"
  ''
