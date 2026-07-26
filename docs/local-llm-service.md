# Design: Family-facing local LLM service on sz1

Status: draft — clarifying. Implementation has not started.

## Context

Stefan wants to host a local LLM chat service so a family member can use it for
long-form creative writing without sending drafts to a third-party service.
sz1 (always-on desktop, sleep disabled) is the host. A separate,
already-merged change adds LM Studio as Stefan's personal experimentation
tool; this design is for the *service* — independent of Stefan's desktop
session.

## Goals

- Browser-based chat UI reachable by one family member from their own device
- Per-user accounts and separate chat histories
- Inference on the GPU (hard requirement)
- Declarative: service topology, ports, and model *intent* live in this repo;
  survives reboots and rebuilds without manual steps
- Model quality suited to long-form creative fiction — primary use case is
  **Witcher fanfic**
- Lore grounding: generation can pull context from downloaded Witcher wiki
  pages (RAG over a local corpus)
- Bounded disk usage: all service state on a dedicated ZFS dataset with a
  200 GB quota, so growth is visible and cleanup is forced

## Non-goals

- Public internet exposure
- Concurrent multi-model serving (one loaded model at a time is fine)
- Admin-proof chat privacy (trust-based within the family is acceptable)
- High availability; if sz1 is off, the service is off
- Replacing LM Studio (it stays as Stefan's personal cockpit)

## Constraints

| Constraint | Consequence |
|---|---|
| GPU is RX 5700 XT (Navi 10 / gfx1010) | No ROCm, no CUDA → **Vulkan is the only GPU path**. This rules out ollama (CPU-only on this card) and mandates llama.cpp built with Vulkan. |
| 8 GB VRAM | Full GPU offload only for models ≤ ~7 GB; larger quants run partially offloaded. Favors 12B-class Q4 models (~7 GB). |
| 39 GB system RAM | Comfortable headroom, including when LM Studio loads a second model concurrently (~7 GB each). |
| LAN-only access | No reverse proxy/TLS/domain needed; firewall opens the UI port on the LAN interface only. |
| NixOS + colmena | All service config in sz1's block in `hive.nix` (host-specific software per repo convention). |

## Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Access path | LAN only | Family member is on the home network. Nothing exposed beyond it. |
| D2 | Privacy model | Trust-based | Standard Open WebUI multi-user; admin could technically read chats and that is accepted. |
| D3 | Backend | `llama-server` (llama.cpp) with Vulkan | Only GPU-capable option on this card. Single-model serving matches stated needs. |
| D4 | Frontend | Open WebUI (`services.open-webui`) | Multi-user auth, chat history, mobile-friendly, OpenAI-API client, packaged as a NixOS module. |
| D5 | LM Studio | Kept, independent | Separate model stores; ~7 GB duplication per shared model is accepted. |
| D6 | Model weights storage | Imperative blob, declarative pointer | Weights live in `/var/lib/llm/models/`; the Nix config references a path + serving parameters. 20 GB blobs don't belong in the Nix store. |
| D7 | Storage substrate | Dedicated ZFS dataset `zpool/llm`, `quota=200G`, mounted at `/var/lib/llm` | `zpool` is sz1's only pool (1.77 T, ~1.3 T free — verified), so "SSD pool" = `zpool`. Hard cap makes disk growth visible and forces cleanup. Holds models, wiki corpus, and (to keep everything under one quota) Open WebUI state. Dataset creation is a documented one-time `zfs create`; the mount is declared in `hardware/sz1.nix` like the existing datasets. |
| D8 | Sharing models between LM Studio and the service | Accept duplication; no sharing mechanism | At ~7–8 GB per model and a handful of models, duplication costs a few tens of GB against 1.3 T free — not worth engineering around. `cp --reflink=always` (OpenZFS block cloning) remains available opportunistically; `dedup=on` is explicitly rejected (pool-wide RAM tax). |
| D11 | Service wiring | nixpkgs modules as-is: `services.llama-cpp` + `services.llama-swap` — no custom units | Researched in the pinned nixpkgs, both fit (see "Module research"). |
| D12 | Accounts | Two: Stefan (admin) + family member; Stefan uses the web UI for chat too | LM Studio stays for model experimentation (D5), but Stefan's day-to-day chat also goes through Open WebUI — same RAG collections and history sync, and exercising the service himself surfaces problems before the family member hits them. |
| D9 | Addressing | `http://sz1.fritz.box:<port>` | FritzBox router DNS already resolves hostnames on the LAN; no mDNS or static-IP management needed. |
| D10 | Corpus scope | Full wiki dump, then curate | Download the complete Fandom XML dump once (cheap, offline), but build the knowledge collection from a curated subset (characters, places, events); iterate on the curation without re-downloading. |

## Architecture

```
family device ──LAN──> Open WebUI (sz1, LAN-bound, auth) ──localhost──> llama-server (Vulkan, one model)
                              │                                              │
                     knowledge collection                     /var/lib/llm/models/*.gguf
                     (Witcher wiki corpus,
                      embeddings, RAG)
                              │
                    /var/lib/llm/{corpus,open-webui}/

                 all of /var/lib/llm = zfs dataset zpool/llm (quota=200G)
```

- `llama-server` bound to `127.0.0.1` only; never directly reachable.
- Open WebUI bound to the LAN interface; its port opened in the sz1 firewall.
- Signup disabled after the two accounts (Stefan + family member) exist.
- Model file, context size, GPU offload layer count, and sampler defaults are
  nix-config values on the llama-server unit.

### Lore grounding (RAG)

Open WebUI's built-in **Knowledge** feature covers the requirement without
extra services: documents are uploaded into a collection, chunked and embedded
locally (default sentence-transformers embedding model, CPU — fine at this
scale), and retrieved chunks are injected into the prompt. A collection can be
attached to a saved "model" preset (base model + system prompt + knowledge), so
the family member just picks e.g. "Witcher Writer" and gets lore-aware chat.

Corpus pipeline (one-time + occasional refresh, scripted):

1. Download Witcher wiki pages (Fandom wikis expose per-page
   `?action=raw` wikitext and full XML dumps via `Special:Statistics`; prefer
   the dump — one request, complete, no crawling).
2. Convert wikitext → plain markdown (strip templates/infoboxes into prose-ish
   text; `pandoc` or a small script).
3. Store under `/var/lib/llm/corpus/witcher/`; upload into the knowledge
   collection via Open WebUI's API (scriptable) or UI.

Retrieval quality knobs (chunk size, top-k) are Open WebUI settings — tune
during evaluation, not in Nix.

## Module research (pinned nixpkgs, verified)

`services.llama-cpp` is sufficient — no custom systemd unit needed:

- `settings` is a freeform attrset mapped 1:1 to `llama-server` CLI flags, so
  model path, `ctx-size`, `n-gpu-layers`, sampler defaults, `flash-attn` are
  all plain Nix config.
- `package` accepts `pkgs.llama-cpp.override { vulkanSupport = true; }` —
  verified to evaluate against the pin (build b10063).
- Unit ships hardened (DynamicUser, `ProtectSystem=strict`,
  `PrivateDevices=false` for GPU). Implications: model files under
  `/var/lib/llm/models` must be world-readable (DynamicUser), and the default
  `RestartSec=300` is worth overriding down (5 min of downtime after a crash is
  needlessly long for a home service).

`services.llama-swap` also exists in the pin (v224): YAML config via
`settings`, per-model `cmd` lines, same GPU-friendly hardening. This is the
evaluation-phase multiplexer — Open WebUI points at llama-swap, which
starts/stops one `llama-server` per requested model. If model switching stays
wanted after evaluation, llama-swap simply remains; otherwise swap in the plain
`services.llama-cpp` unit for the winner.

llama-swap constraints we rely on:

- **Never configure llama-swap `groups`/concurrency**: two ~7 GB models cannot
  share 8 GB VRAM; a concurrently started second model lands on CPU and
  silently halves performance. One-at-a-time (the default) is mandatory here.
- Swaps are per-request: two users chatting with different models cause a
  multi-second model reload on every alternating message. One 8 GB GPU means
  "one model at a time" is household-wide, not per-user.

## Model strategy

- Laguna XS-2.1 (coding-tuned) is explicitly the wrong model for this use case.
- Candidate pool: 12B-class creative/RP fine-tunes (Mistral-Nemo family and
  similar), shortlisted via EQ-Bench creative writing +
  r/SillyTavernAI community consensus. Q4 quant ≈ 7 GB → full GPU offload.

### Shortlist (July 2026, all Mistral-Nemo-based, all Q4_K_M ≈ 7.5 GB)

| Candidate | GGUF source | Profile | Template / samplers |
|---|---|---|---|
| **MN-12B-Mag-Mell-R1** | `inflatebot/MN-12B-Mag-Mell-R1-GGUF` (official) | Long-standing community favorite for prose quality and worldbuilding; "best of Nemo" multi-merge. | ChatML; temp 1.25 + minP 0.2; no XTC, DRY sparingly. Author notes stability to ~10k tokens — keep serving ctx modest (~16k). |
| **Rocinante-12B-v1.1** | `TheDrummer/Rocinante-12B-v1.1-GGUF` (official, 7.48 GB) | Adventure-flavored storytelling; the line is tuned for sustained narrative rather than assistant-style replies. | ChatML (RP) or Mistral; temp 0.7 stable / 1.2 creative; DRY recommended. |
| **Ayla-Light-12B-v2** | `mradermacher/Ayla-Light-12B-v2-GGUF` (7.48 GB) | Strong prose quality for its size class while retaining instruction following. | Per model card; start temp ~1.0 + minP 0.1. |

All three fit fully in 8 GB VRAM at Q4_K_M with room for KV cache at ~16k
context. Stretch option if all three disappoint on prose *and* the performance
bar shows headroom: a Mistral-Small-based ~24B tune (e.g. TheDrummer's
Cydonia line) at Q4 ≈ 13 GB, partially offloaded — expect roughly half the
speed; benchmark before promising it to anyone.
- Evaluation: family member scores 2–3 candidates on identical prompts
  (prose quality, positivity bias, slop, long-session degradation) — see
  conversation notes; sampler settings per model card, not defaults. Include at
  least one prompt that depends on retrieved Witcher lore, so candidates are
  judged with RAG in the loop (models differ in how well they use injected
  context vs hallucinate over it).
- Switching the served model = swap the GGUF path in config + rebuild. If model
  switching turns out to be frequent, evaluate `llama-swap` (proxy that
  starts/stops llama-server instances per requested model) as a later addition.

## Performance bar

- ≥ 8 tok/s decode on the chosen model with realistic chat context — verified
  with `llama-bench` under Vulkan before the frontend goes live.
- If a 12B Q4 with full offload misses the bar (unlikely), fall back to smaller
  quant or smaller model rather than CPU inference.

## Risks & unknowns

| Risk | Mitigation |
|---|---|
| Vulkan performance on gfx1010 is unverified for the chosen model | Benchmark phase before committing (performance bar above). |
| RAM/VRAM contention when LM Studio runs simultaneously | VRAM: both cannot fully offload at once; acceptable — Stefan's experiments briefly degrade the service. Document, don't engineer around. |
| Open WebUI is a fast-moving package | Pinned via nixpkgs like everything else; updates arrive with `npins update` and are verified by `colmena build`. |
| Family device discovery of sz1's address | Static IP or mDNS (`sz1.local`); pick during implementation. |

## Implementation plan (proposed phases)

0. **Storage**: create `zpool/llm` (`quota=200G`), declare the mount in
   `hardware/sz1.nix`, check `feature@block_cloning` is enabled on the pool.
1. **Backend**: Vulkan-built llama.cpp behind `services.llama-swap`, candidate
   models in `/var/lib/llm/models`, benchmark against the performance bar.
2. **Frontend**: Open WebUI wired to the backend, LAN firewall opening,
   accounts created, signup disabled.
3. **Corpus**: Witcher wiki dump → markdown → knowledge collection; save the
   fetch/convert script in the repo; "Witcher Writer" preset wired to it.
4. **Evaluation**: candidate models tested by the family member via the UI's
   side-by-side compare; winner becomes the declared default.
5. **Polish**: sampler defaults per model, mDNS/static address, brief usage
   note for the family member.

## Open questions

- [ ] Which curation heuristic for the wiki subset (namespace/category filters
      vs hand-picked page list)?
- [ ] Does Open WebUI's state directory relocate cleanly to `/var/lib/llm/`
      (module option vs bind mount)?
