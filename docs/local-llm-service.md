# Design: Family-facing local LLM service on sz1

Status: in progress — phases 0, 0.5, 1 and 2 complete; phase 2.5 (edge) config written and build-verified, pending: real `inwx.age` credentials (placeholder committed), FritzBox 443→t20 forward, t20 deploy.

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

- Exposing anything beyond the authenticated UI (the inference API itself
  stays private; only Caddy→Open WebUI is reachable from outside)
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
| Remote access from phone + work laptop, browser-only | Public HTTPS edge on t20 (see D1/D14). sz1's Open WebUI port stays LAN-bound; only t20's Caddy faces the internet. |
| NixOS + colmena | All service config in sz1's block in `hive.nix` (host-specific software per repo convention). |
| t20 is a Pi 3: **1 GB RAM**, ext4 on SD card, built via aarch64 emulation on sz1 | Caps what the edge can host. Retiring Ghidra server (D15) frees ~400 MB and makes room for Caddy plus a small IdP; it rules out anything needing PostgreSQL (D16). Emulated builds also favor Go over large Rust closures. Everything on t20 gates every service, on storage that wears out — back up edge state before other services depend on it. **Planned:** move t20's root to an external SSD, which also makes it a NAS. Note the Pi 3 ceiling before sizing that: USB and Ethernet share one USB 2.0 bus, so aggregate throughput is roughly 25–40 MB/s no matter how fast the SSD is. Good enough for edge state and backups; not a fileserver for large media. |
| Primary user is ~70/30 phone/desktop (Stefan's estimate, ~90% confidence — confirm in interview); part of the 30% is a **work laptop in the office** | Frontend must be mobile-first: favors Open WebUI's chat UI, weighs against SillyTavern (poor on mobile) and document-centric harnesses unless they have a real mobile story. D13's bible-update flow must be skim-and-approve simple on a phone. Remote access is a hard requirement (drove the D1 revision), and the work laptop means **browser-only, zero client install** — no WireGuard profile on a managed device; public HTTPS on 443 is the one thing corporate networks reliably pass. |

## Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Access path | **Revised:** public HTTPS via t20 edge (supersedes "LAN only") | Original LAN-only choice quietly meant "only works at home" — contradicted by the ~70% phone usage (see Constraints). New shape: DynDNS name → FritzBox forwards 443 → Caddy on t20 (always-on Pi 3, TLS via Let's Encrypt) → reverse-proxy to Open WebUI on sz1. Bonus: real HTTPS gives the phone a secure context → installable PWA (resolves the app-vs-bookmark caveat). Trade-off accepted knowingly: Open WebUI's login becomes the public perimeter; mitigations = signup disabled, strong passwords, Caddy rate-limit on auth endpoints, pins kept fresh. WireGuard remains the fallback if this proves noisy. **Verify first: FritzBox has public IPv4 (not DS-Lite/CGNAT), else this needs a relay and the decision reopens.** |
| D2 | Privacy model | Trust-based | Standard Open WebUI multi-user; admin could technically read chats and that is accepted. |
| D3 | Backend | `llama-server` (llama.cpp) with Vulkan | Only GPU-capable option on this card. Single-model serving matches stated needs. |
| D4 | Frontend | Open WebUI (`services.open-webui`) — **as a probe, not a verdict** | Multi-user auth, chat history, mobile-friendly, OpenAI-API client, packaged as a NixOS module. Crucially, the primary user's actual writing workflow is unvalidated (see Risks): Open WebUI is the cheapest frontend that serves both chat-style co-writing and document-style drafting acceptably, with zero client-device setup. The OpenAI-API boundary makes frontends swappable, so this decision is designed to be revised after real use — SillyTavern (chat-native RP) or a document-centric harness are later iterations against the same backend, not redesigns. |
| D5 | LM Studio | Kept, independent | Separate model stores; ~7 GB duplication per shared model is accepted. |
| D6 | Model weights storage | Imperative blob, declarative pointer | Weights live in `/var/lib/llm/models/`; the Nix config references a path + serving parameters. 20 GB blobs don't belong in the Nix store. |
| D7 | Storage substrate | Dedicated ZFS dataset `zpool/llm`, `quota=200G`, mounted at `/var/lib/llm` | `zpool` is sz1's only pool (1.77 T, ~1.3 T free — verified), so "SSD pool" = `zpool`. Hard cap makes disk growth visible and forces cleanup. Holds models, wiki corpus, and (to keep everything under one quota) Open WebUI state. Dataset creation is a documented one-time `zfs create`; the mount is declared in `hardware/sz1.nix` like the existing datasets. |
| D8 | Sharing models between LM Studio and the service | Accept duplication; no sharing mechanism | At ~7–8 GB per model and a handful of models, duplication costs a few tens of GB against 1.3 T free — not worth engineering around. `cp --reflink=always` (OpenZFS block cloning) remains available opportunistically; `dedup=on` is explicitly rejected (pool-wide RAM tax). |
| D9 | Addressing | **Revised:** `chat.home.zabka.it`; `*.home.zabka.it` resolves to the home public IPv4 (dynamic-DNS-updated A record); `sz1.fritz.box` remains the internal upstream t20 proxies to | Superseded LAN-only answer was `http://sz1.fritz.box:<port>`. Everyone — home or remote — uses `https://chat.home.zabka.it/`; FritzBox NAT hairpinning makes the public name work from inside the LAN, so one URL, one PWA origin. Base renamed from `t20.zabka.it` to `home.zabka.it` (2026-07-28): a location- and host-neutral name survives the upcoming move and any future proxy-host swap — only the DynDNS record changes. The `*.home` scheme leaves room for sibling services (e.g. a status/wake vhost). DNS host is **INWX**: FritzBox updates `home.zabka.it` via INWX's DynDNS endpoint (second entry alongside Stefan's existing one from another FritzBox); `*.home.zabka.it` is a static CNAME to it. Certs: DNS-01 **wildcard** for `*.home.zabka.it` via the official `caddy-dns/inwx` plugin (`pkgs.caddy.withPlugins`, `services.caddy.package`) — keeps `chat.*` out of Certificate Transparency logs, which otherwise make named hosts enumerable within minutes of issuance. INWX API credentials as an agenix secret on t20 (established repo pattern); prefer a dedicated DNS-scoped API user over the main registrar login, which also sidesteps the TOTP `shared_secret` requirement. **Verified 2026-07-28: public IPv4 (79.197.182.183, dual-stack) — no DS-Lite; DynDNS A record confirmed working 2026-08-02.** Address family (corrected 2026-08-02 — an earlier IPv4-only note here was Claude's recommendation recorded without Stefan's sign-off): **dual-stack**. Stefan's direction is IPv6-forward (IPv6-only if it were viable), but the work laptop on a corporate network — the constraint that drove the public-443 design — is the canonical IPv4-only client, so v4 stays until that constraint dies. AAAA must point at **t20's own global address**, never the router's (the FritzBox `<ip6addr>` placeholder substitutes the router — the accidental AAAA it published is wrong-host and gets deleted): t20 maintains its own AAAA via the INWX API (same agenix credentials as the DNS-01 wildcard cert, small systemd timer, survives prefix rotation), plus a FritzBox IPv6 firewall exception for t20:443. Interface ID pinned to `::443` (decided 2026-08-06): stable-privacy SLAAC derives the ID per prefix, so a reconnect or the move would silently invalidate the FritzBox rule; a NetworkManager `ipv6.token` (with `addr-gen-mode eui64`) makes t20 always `<prefix>::443`, so the FritzBox Freigabe and the AAAA logic never need re-reading the host. |
| D10 | Corpus scope | Full wiki dump, then curate | Download the complete Fandom XML dump once (cheap, offline), but build the knowledge collection from a curated subset (characters, places, events); iterate on the curation without re-downloading. |
| D11 | Service wiring | nixpkgs modules as-is: `services.llama-cpp` + `services.llama-swap` — no custom units | Researched in the pinned nixpkgs, both fit (see "Module research"). |
| D12 | Accounts | Two: Stefan (admin) + family member; Stefan uses the web UI for chat too | LM Studio stays for model experimentation (D5), but Stefan's day-to-day chat also goes through Open WebUI — same RAG collections and history sync, and exercising the service himself surfaces problems before the family member hits them. |
| D13 | Long-form persistence | Lossless external canon: never destroy, extract cheaply, curate optionally | The primary user's complaint about hosted Claude: established facts silently fail to persist through compaction and must be *restated by hand* — nothing is committed to durable notes. The fix is losslessness + low-effort recall, not manual curation for its own sake: (1) full transcripts persist verbatim in Open WebUI — local disk is free, so unlike API compaction nothing is ever destroyed; worst case a missed fact is *copied* from an old chat, never reconstructed from memory. (2) A per-story "story bible" knowledge collection (characters, plot state, established facts, chapter summaries) is the retrieval index RAG injects per message; one chat per chapter keeps the ~16k window for the active scene. (3) Bible updates are model-drafted at end of chapter (extraction, one click) with the author skimming/correcting — extraction misses are recoverable indexing gaps, not data loss, which is what makes automation safe here where compaction isn't. Serving ctx ~16k with quantized KV cache + flash attention (KV competes with weights for 8 GB VRAM; Nemo-class quality degrades past ~16k regardless). Escalation: SillyTavern (keyed lorebooks, auto-summary) against the same backend. |
| D14 | t20 edge service | Caddy on t20 (always-on) + a small status service | t20 terminates TLS and reverse-proxies to sz1 (SSE streaming works out of the box). When sz1 is unreachable, t20 diagnoses *why* and serves the right page: port answers → proxy; host pings but port closed → "sz1 is in Windows right now"; no ping → powered off → "sz1 is off — ask Stefan". **WoL deliberately out of scope** (2026-07-28: simplifies the edge and the upcoming move; the wake-button design lives in git history if ever wanted). Pick an innocuous DynDNS hostname — it will appear in DNS and proxy logs on networks we don't control. |
| D15 | Ghidra server on t20 | **Retired** (2026-08-02) | Ghidra now runs fully locally, so the JVM (256 M heap, ~400 MB RSS) no longer has to share t20's 1 GB with the edge. Frees the headroom D16 needs; also removes the only other stateful service competing for the SD card. |
| D16 | Single sign-on | **Authelia on t20**, added after the edge (phase 2.6); Open WebUI is the first consumer | Goal is "one login, and future services are cheap to add". Costs nothing in DNS or certificates: `auth.home.zabka.it` is already covered by the `*.home.zabka.it` wildcard CNAME and DNS-01 cert, and the whole OIDC flow rides the single 443 forward from D1 — no extra hole punching, and `sz1.fritz.box` never appears in it. Authelia is chosen over the alternatives on fit, not preference: **Zitadel rejected** — it requires PostgreSQL, and Zitadel + Postgres is ~600 MB against a 1 GB Pi 3 (module exists at v2.71.7 in the pin; wrong hardware, not wrong software). **Kanidm** is the better directory (proper user/group model, passkeys) and stays the fallback if a real directory is ever wanted, but it does not do forward-auth, insists on an `https` origin, and is a large Rust build under aarch64 emulation. Authelia does OIDC **and** forward-auth on SQLite in ~50–100 MB, sits behind a proxy on plain HTTP, and Caddy's `forward_auth` is a native directive. Forward-auth is the part that makes future services cheap: it puts a login in front of services that have no auth at all. Session cookie scoped to `.home.zabka.it` is what makes one login cover every subdomain — another dividend of D9's wildcard. **Not yet signed off: Authelia vs Kanidm was Claude's recommendation; Stefan approved recording it, not the tool choice.** |

## Architecture

```
phone / work laptop (browser, PWA)
        │ https://<dyndns-domain>/  (443)
        ▼
FritzBox ──port-forward──> t20: Caddy (TLS, rate-limit)──┐
                            │                            │ sz1 reachable?
                            │ sz1 down: status page      │
                            │  - pings, port closed →    │
                            │    "sz1 is in Windows"     │
                            │  - no ping → "sz1 is off" │
                            ▼                            ▼
                       (static page)      Open WebUI (sz1, LAN-bound, auth)
                                                │               │
                                       knowledge collections    │ localhost
                                       (Witcher wiki corpus,    ▼
                                        story bibles, RAG)   llama-swap ──spawns──> llama-server
                                                │                                  (Vulkan, one model
                                                ▼                                   at a time)
                                     /var/lib/llm/{corpus,open-webui}/                  │
                                                                          /var/lib/llm/models/*.gguf

                          all of /var/lib/llm = zfs dataset zpool/llm (quota=200G)
```

- `llama-swap`/`llama-server` bound to `127.0.0.1` only; never directly reachable.
- Open WebUI bound to the LAN interface; its port open in the sz1 firewall for
  t20 and LAN clients, never forwarded by the router.
- Only t20:443 is internet-facing; Caddy terminates TLS (Let's Encrypt) and
  rate-limits the auth endpoints.
- Signup disabled after the two accounts (Stefan + family member) exist.
- Model file, context size, GPU offload layer count, and sampler defaults are
  nix-config values on the llama-swap model entries.

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

## User research findings (2026-07, first pass)

From the primary user, via Stefan (not from reading chats):

- **Workflow is author-native drafting, not roleplay**: they either write tiny
  story fragments for the model to enrich with detail, or toss in an idea and
  have the model write the story. Consequences: evaluation uses seed→story and
  fragment→enrichment tasks (not RP dialogue); the "Witcher Writer" preset is
  an enrichment prompt (expand what's given, preserve established facts and
  the author's voice, don't rush endings), not a persona; Rocinante should be
  A/B'd in its Alpaca Story/Instruct mode against ChatML, since instruct-style
  story generation is exactly its Alpaca mode.
- **Open follow-up**: where does the finished story text live after
  generation? If they already paste into a doc, that doc is the embryonic
  story bible (validates D13) and the informal version of the two-layer
  chat + story-file structure.

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

**GGUF chat-template defects (verified 2026-08-02).** Two of the three quants
ship a broken `tokenizer.chat_template`, in different ways. Both are fixed by
passing `--chat-template chatml` explicitly, which replaces the embedded
template before it is ever rendered — verified via llama-server's
`/apply-template` endpoint, which returns the exact prompt string and is the
fastest way to check this on any future candidate.

- **Ayla-Light-12B-v2**: embedded template is real Jinja but uses a test
  llama.cpp's engine lacks (`selectattr(..., "tool_calls")`). A template parse
  error is **fatal** — the model refused to start at all until overridden.
- **Rocinante-12B-v1.1**: embedded template field contains the template *name*
  `mistral-v7-tekken` instead of a template body. Rendered as Jinja, a string
  with no placeholders yields itself, so the entire prompt became the literal
  text "mistral-v7-tekken" and the user's message was silently dropped. Fails
  quietly with plausible-looking garbage rather than erroring.

Rocinante has a second, independent quirk: ChatML markers are **not** in its
vocab (`<|im_end|>` tokenizes to six ordinary text tokens), so the model emits
them as visible text and llama.cpp cannot stop on them. Generation still
terminates correctly on the real `</s>`, so this is cosmetic — a trailing
`<|im_end|>` in the rendered reply. Fix is a string stop sequence in the Open
WebUI model preset; llama-server has no CLI flag for default stops. This raises
the stakes on the ChatML-vs-Mistral A/B below: the Mistral path uses `[INST]`
and `</s>`, which are real vocab tokens, and would drop the workaround.

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

**Measured (2026-07-27, Mag-Mell Q4_K_M, Vulkan, fa=1): bar cleared.**
Generation peaks at `-ngl 32` with **12.9 tok/s** (24→7.9, 28→9.9, 32→12.9,
36→10.7, 41→10.7 — beyond 32 layers the 8 GB VRAM oversubscribes and the
driver spills, so full offload is *slower*). Shipped config already uses 32.
Live serving smoke test: ~18 tok/s decode, ~48 tok/s prompt processing.
Known future pain: prefill at ~50-60 tok/s means a multi-thousand-token
RAG-loaded first message waits 30-60 s before the first token — keep phase-3
retrieval injection lean; llama-server prompt caching covers repeat turns.
Curiosity for later: prefill hit 228 tok/s at `-ngl 24` (VRAM headroom helps
prompt batching) — a prefill-vs-decode trade exists if prefill ever dominates.

## Risks & unknowns

| Risk | Mitigation |
|---|---|
| Vulkan performance on gfx1010 is unverified for the chosen model | Benchmark phase before committing (performance bar above). |
| RAM/VRAM contention when LM Studio runs simultaneously | VRAM: both cannot fully offload at once; acceptable — Stefan's experiments briefly degrade the service. Document, don't engineer around. |
| Open WebUI is a fast-moving package — and now the **public perimeter** | Pinned via nixpkgs like everything else; updates arrive with `npins update` and are verified by `colmena build`. Being internet-facing raises the stakes on keeping the pin fresh; Caddy rate-limiting buys brute-force protection, not CVE protection. WireGuard fallback stays documented if this proves uncomfortable. |
| **SSO couples availability** (D16). Today a WAN outage only costs the public name; LAN clients still reach `http://sz1.fritz.box:8080` with local accounts. Once login goes through an issuer at `auth.home.zabka.it`, Open WebUI's server-side token exchange needs public DNS too — a WAN outage becomes a *total* outage, including at home. | Keep one local admin account that bypasses SSO as the break-glass path, and treat a local DNS override for `*.home.zabka.it` as the real fix if outages prove annoying. Decide this deliberately at 2.6 rather than discovering it during one. |
| **Trusted-header auth is only as strong as the network path** (D16 open question). If Open WebUI is reached bypassing Caddy, anyone who can hit sz1:8080 can assert `Remote-Email` and become admin. The phase-2 firewall rule currently allows the whole LAN. | Either choose OIDC for Open WebUI (tokens are verified, so direct access gains nothing), or narrow the sz1:8080 rule to t20 alone — which also removes the LAN fallback above. Do not adopt trusted headers while the LAN-wide rule stands. |
| **Work-laptop privacy is outside our control** | A managed corporate device may have TLS inspection or endpoint monitoring; our Let's Encrypt TLS does not protect content from the device's own employer. This must be communicated honestly to the primary user: on the work laptop, treat the service as employer-visible regardless of our architecture. Phone on mobile data is the private path. |
| **Primary user's writing workflow is unvalidated.** Stefan has promised not to read their chats, so whether they co-write turn-by-turn (chat-native) or draft prose with model assistance (document-native) is unknown — and D13's bible/chapter workflow may feel like homework to them. | User research = asking, not reading: a short interview about how they work with Claude today and what compaction loses, before/during evaluation. Open WebUI ships as a probe (D4); frontend is swappable behind the API boundary if the workflow verdict demands it (SillyTavern for chat-native, document-centric harness for author-native). |

## Implementation plan (proposed phases)

0. **Storage** — ✅ done 2026-07-26: `zpool/llm` created (200G quota),
   mounted at `/var/lib/llm` via `hardware/sz1.nix`, deployed and verified.
0.5. **Edge preflight** — public IPv4 ✅ (79.197.182.183, 2026-07-28); DynDNS
   A record ✅ + `*.home.zabka.it` wildcard CNAME ✅ (both verified against
   INWX authoritative NS, 2026-08-02; AAAA stripped pending the t20
   self-update design in D9). WoL dropped from scope. DNS-scoped INWX API
   user (DNS-management role only) ✅ created 2026-08-02. **Preflight complete**;
   the credential lands in agenix when phase 2.5 builds the t20 edge.
1. **Backend** — ✅ done 2026-07-27: `services.llama-swap` on localhost:9292,
   three candidates behind Vulkan llama-server, models downloaded, benchmark
   cleared the bar at `-ngl 32` (see Performance bar), smoke test returned a
   live completion through the swap proxy.
2. **Frontend** — ✅ done 2026-08-02: `services.open-webui` on sz1:8080 against
   llama-swap, firewall opened on the LAN NIC only (a global rule would also
   expose it on the wg-sect tunnel), accounts created from the admin panel.
   State lives on `zpool/llm/open-webui` mounted at `/var/lib/private/open-webui`
   — the unit hardcodes `StateDirectory` and runs with `DynamicUser`, so a
   `stateDir` under `/var/lib/llm` would need a chown to a runtime-allocated
   UID; a child dataset keeps it inside the 200 G quota instead (ZFS quotas
   bound descendants). Self-registration is disabled permanently: the admin
   panel creates users, so signup is never needed. See
   `scripts/deploy-llm-phase2.sh`.
2.5. **Edge**: Caddy + status service on t20 (colmena remote deploy),
   FritzBox 443 forward + DynDNS update for `*.home.zabka.it`, rate limits,
   then the PWA smoke test from a phone on mobile data.
2.6. **SSO** (D16): retire Ghidra server from t20, add Authelia as a sibling
   vhost behind the same Caddy, then migrate Open WebUI to it as the first
   consumer — keeping one local admin account as the break-glass path. Needs
   the edge to exist first; building it before 2.5 means building TLS twice.
3. **Corpus**: Witcher wiki dump → markdown → knowledge collection; save the
   fetch/convert script in the repo; "Witcher Writer" preset wired to it.
4. **Evaluation**: two things under test, not one — (a) candidate models via
   the UI's side-by-side compare, (b) the harness itself: workflow interview
   with the primary user, then does the bible/chapter pattern fit how they
   actually write? Winner model becomes the declared default; harness verdict
   decides whether a frontend iteration (SillyTavern / document-centric tool)
   is warranted.
5. **Polish**: sampler defaults per model, brief usage note for the family
   member (URL `https://chat.home.zabka.it/`, accounts, story-bible how-to).

## Open questions

- [ ] Which curation heuristic for the wiki subset (namespace/category filters
      vs hand-picked page list)?
- [x] Does Open WebUI's state directory relocate cleanly to `/var/lib/llm/`?
      No — `StateDirectory` is hardcoded and `DynamicUser` is on. Resolved with
      a child dataset mounted at `/var/lib/private/open-webui` (phase 2).
- [x] Open WebUI against Authelia: **OIDC**, decided 2026-08-02. Open WebUI has
      its own user model, tokens are verified so a direct hit on sz1:8080 gains
      nothing, and group→role mapping comes free. Forward-auth stays the tool
      for services that have no auth of their own. Consequence: the sz1:8080
      firewall rule can stay LAN-wide, which preserves the local fallback.
- [ ] Authelia or Kanidm (D16)? Recorded as Authelia on fit; not yet signed off.
