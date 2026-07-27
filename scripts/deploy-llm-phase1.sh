#!/usr/bin/env bash
# Phase 1 of docs/local-llm-service.md: deploy the llama-swap backend, fetch
# the three candidate models (~23 GB), then benchmark GPU offload and smoke-
# test the API. Run OUTSIDE the sandbox; prompts for sudo.
# Output captured to deploy-phase1.log.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
{
  set -x
  # 1. Deploy (llama-swap service, tmpfiles dirs, llama-bench on PATH)
  bash rebuild.sh || exit 1

  # 2. Fetch models (idempotent, resumes; ~23 GB on first run)
  sudo bash scripts/fetch-llm-models.sh || exit 1

  # 3. Offload tuning sweep: tokens/sec at different GPU layer counts.
  #    tg = generation speed (the ≥8 tok/s bar), pp = prompt processing.
  llama-bench -m /var/lib/llm/models/MN-12B-Mag-Mell-Q4_K_M.gguf \
    -ngl 24,28,32,36,41 -fa 1 || exit 1

  # 4. Smoke-test llama-swap: model list, then one tiny completion
  #    (first call swap-loads the model; give it time).
  curl -sf http://127.0.0.1:9292/v1/models || exit 1
  echo
  curl -sf http://127.0.0.1:9292/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"mag-mell","messages":[{"role":"user","content":"Say hello in one short sentence."}],"max_tokens":32}' || exit 1
  echo
} 2>&1 | tee deploy-phase1.log
exit "${PIPESTATUS[0]}"
