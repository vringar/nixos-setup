#!/usr/bin/env bash
# Sweep --n-gpu-layers under the *serving* configuration.
#
# The earlier sweep used llama-bench, which allocates no server slots and so
# measured a KV cache a fraction of the size the service actually holds: with
# --parallel N each slot keeps its own cache of the full context, so the real
# footprint is N times larger and can spill into system memory over PCIe.
# This runs llama-server with the production flags instead and reports the
# spill alongside the speed, because a configuration that is fast in isolation
# and spilling in production is the trap we already fell into.
#
# Takes the GPU exclusively: llama-swap is stopped for the duration and
# restarted on exit, including on interrupt. Run it when nobody is chatting.
set -euo pipefail

MODEL="${MODEL:-/var/lib/llm/models/Rocinante-12B-v1.1-Q4_K_M.gguf}"
CTX="${CTX:-12288}"
PARALLEL="${PARALLEL:-2}"
LAYERS="${LAYERS:-28 32 36 40}"
PORT=5999
LOG="benchmark-ngl-$(date +%Y%m%d-%H%M%S).log"

# 200 tokens is long enough for the generation rate to settle and short enough
# that a four-point sweep does not take all evening.
PROMPT='Write a detailed description of a rainy evening in the city of Novigrad.'
NPREDICT=200

vram() { cat /sys/class/drm/card*/device/mem_info_vram_used 2>/dev/null | head -1; }
gtt() { cat /sys/class/drm/card*/device/mem_info_gtt_used 2>/dev/null | head -1; }
mib() { echo $(($1 / 1048576)); }

server_pid=""
cleanup() {
  [ -n "$server_pid" ] && kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  echo "restarting llama-swap..."
  sudo systemctl start llama-swap || true
}
trap cleanup EXIT INT TERM

{
  echo "model:    $MODEL"
  echo "ctx:      $CTX   parallel: $PARALLEL   layers swept: $LAYERS"
  echo

  echo "stopping llama-swap to free the GPU..."
  sudo systemctl stop llama-swap
  sleep 3
  base_vram=$(vram)
  echo "baseline VRAM in use (desktop etc.): $(mib "$base_vram") MiB"
  echo

  printf '%-6s %-10s %-10s %-12s %-12s %s\n' \
    ngl vram_MiB gtt_MiB prompt_tok_s gen_tok_s note

  for ngl in $LAYERS; do
    llama-server --port "$PORT" -m "$MODEL" \
      --n-gpu-layers "$ngl" --ctx-size "$CTX" --parallel "$PARALLEL" \
      --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --no-webui \
      --chat-template chatml >/dev/null 2>&1 &
    server_pid=$!

    ready=no
    for _ in $(seq 1 120); do
      if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        ready=yes
        break
      fi
      sleep 2
    done
    if [ "$ready" != yes ]; then
      printf '%-6s %-10s %-10s %-12s %-12s %s\n' "$ngl" - - - - "failed to start"
      kill "$server_pid" 2>/dev/null || true
      wait "$server_pid" 2>/dev/null || true
      server_pid=""
      continue
    fi

    v=$(mib "$(vram)")
    g=$(mib "$(gtt)")

    # /completion reports its own timings, which beats measuring wall clock
    # around a streaming response.
    result=$(curl -fsS "http://127.0.0.1:$PORT/completion" \
      -H 'Content-Type: application/json' \
      -d "{\"prompt\": $(printf '%s' "$PROMPT" | jq -Rs .), \"n_predict\": $NPREDICT, \"stream\": false}")
    pps=$(echo "$result" | jq -r '.timings.prompt_per_second // 0 | floor')
    gps=$(echo "$result" | jq -r '.timings.predicted_per_second // 0 | floor')

    note=""
    [ "$g" -gt 200 ] && note="SPILLING to system RAM"

    printf '%-6s %-10s %-10s %-12s %-12s %s\n' "$ngl" "$v" "$g" "$pps" "$gps" "$note"

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    server_pid=""
    sleep 3
  done

  echo
  echo "Pick the highest ngl whose gtt stays near the baseline: spilling costs"
  echo "more than the extra offloaded layers gain."
} 2>&1 | tee "$LOG"

echo
echo "log written to $LOG"
