# The family-facing local LLM service on sz1: inference backend, Open WebUI
# frontend, and the jobs that reconcile database-backed state into it.
# See docs/local-llm-service.md for the design and the decision record.
#
# sz1-only by construction — imported from that host's block rather than from
# `defaults`, the same way modules/wg-sect.nix is. Nothing here is
# parameterised, because there is exactly one machine with this GPU and this
# dataset layout; a second consumer is when to introduce options.
{
  pkgs,
  lib,
  config,
  httpReady,
  ...
}: let
  llamaCppVulkan = pkgs.llama-cpp.override {vulkanSupport = true;};
  # One source of truth: the server is started with this, and the usage
  # plugin is told the same number, so the context meter cannot drift from
  # the context actually served.
  ctxSize = 12288;
  parallelSlots = 2;
  openWebUi = pkgs.callPackage ../apps/openweb-ui {};
  # Shared llama-server invocation for llama-swap model entries.
  # \${PORT} stays literal for Nix; llama-swap substitutes it at spawn time.
  # --n-gpu-layers/--ctx-size/KV-cache quant are initial guesses for the
  # 8 GB RX 5700 XT; tuned via the phase-1 llama-bench run
  # (see docs/local-llm-service.md).
  serve = model: flags:
    lib.concatStringsSep " " ([
        (lib.getExe' llamaCppVulkan "llama-server")
        "--port \${PORT}"
        "-m /var/lib/llm/models/${model}"
        "--n-gpu-layers 32"
        "--ctx-size ${toString ctxSize}"
        # Slots each hold their own KV cache of ctxSize (the cache is not
        # unified by default), so this multiplies VRAM rather than dividing
        # the context. Auto-detection picked 4 and pushed ~1 GB of KV into
        # system memory over PCIe; 2 matches the number of people and fits.
        "--parallel ${toString parallelSlots}"
        "--flash-attn on"
        "--cache-type-k q8_0"
        "--cache-type-v q8_0"
        "--no-webui"
      ]
      ++ flags);
in {
  # --- Family LLM service backend (docs/local-llm-service.md, phase 1) ---
  # One model at a time on the 8 GB GPU; llama-swap swaps llama-server
  # instances per requested model. Never configure llama-swap `groups`
  # (concurrent models would spill to CPU and silently halve performance).
  services.llama-swap = {
    enable = true;
    # Module default listenAddress is localhost; never expose this port.
    port = 9292;
    settings = {
      # First request after a swap loads ~7.5 GB from disk — allow it.
      healthCheckTimeout = 300;
      models = {
        # Sampler settings per model card (see design doc shortlist).
        mag-mell = {
          cmd = serve "MN-12B-Mag-Mell-Q4_K_M.gguf" [
            "--chat-template chatml"
            "--temp 1.25"
            "--min-p 0.2"
          ];
          ttl = 1800;
        };
        rocinante = {
          cmd = serve "Rocinante-12B-v1.1-Q4_K_M.gguf" [
            "--chat-template chatml"
            "--temp 1.0"
          ];
          ttl = 1800;
        };
        ayla-light = {
          cmd = serve "Ayla-Light-12B-v2.Q4_K_M.gguf" [
            # The template baked into this GGUF uses a Jinja test llama.cpp's
            # engine lacks (`selectattr(..., "tool_calls")`), and a template
            # parse error is fatal, not a fallback. Overriding it sidesteps
            # the parse entirely — same reason the other two carry this flag.
            "--chat-template chatml"
            "--temp 1.0"
            "--min-p 0.1"
          ];
          ttl = 1800;
        };
      };
    };
  };

  # Open WebUI needs a while to come up on first start (migrations, model
  # list), so allow far more retries than the default and raise the start
  # timeout above the resulting worst case.
  systemd.services.open-webui.serviceConfig = {
    ExecStartPost = httpReady {
      inherit pkgs;
      url = "http://127.0.0.1:8080/health";
      retries = 60;
    };
    TimeoutStartSec = "900s";
  };

  # --- Lore corpus (docs/local-llm-service.md, phase 3) ---
  # Knowledge collections live in Open WebUI's database, so they cannot be
  # declared outright. This reconciles their *contents* instead: the corpus
  # derivation is the desired state and the service makes the collection
  # match it on every activation. Idempotent — a second run uploads nothing.
  age.secrets.open-webui-token.file = ../secrets/open-webui-token.age;
  systemd.services.witcher-corpus = {
    description = "Reconcile the Witcher lore corpus into Open WebUI";
    # open-webui only counts as started once /health answers (see its
    # ExecStartPost), so ordering after it is meaningful rather than a race.
    after = ["open-webui.service" "network-online.target"];
    wants = ["network-online.target"];
    requires = ["open-webui.service"];
    # Started by the timer below, never by a target. A oneshot counts as
    # activating until its process exits, so anything that starts this unit
    # waits for the whole corpus to embed — which made `colmena apply` hang
    # for hours on any change to the script or the corpus derivation.
    serviceConfig = {
      Type = "oneshot";
      EnvironmentFile = config.age.secrets.open-webui-token.path;
      # Embedding 5.5k documents is CPU work competing with inference; the
      # corpus changes rarely, so let it lose the race.
      Nice = 15;
      # A missing collection is not urgent enough to wake anyone at 3am, but
      # a transient failure right after boot should not need a manual rerun.
      Restart = "on-failure";
      RestartSec = "5min";
    };
    script = ''
      exec ${pkgs.python3.withPackages (ps: [ps.requests])}/bin/python3 \
        ${../apps/witcher-corpus/reconcile.py} \
        witcher-lore ${pkgs.callPackage ../apps/witcher-corpus {}}
    '';
  };

  # Decouples the run from activation, and reconciles drift on a schedule
  # rather than only at boot: the corpus is a derivation, so a rebuild that
  # changes it gets picked up within a day without anyone deploying again.
  # Monotonic rather than OnCalendar, so Persistent= would do nothing here —
  # sz1 stays up, and a missed run costs at most a day of staleness.
  systemd.timers.witcher-corpus = {
    description = "Schedule the Witcher lore corpus reconciliation";
    wantedBy = ["timers.target"];
    timerConfig = {
      # Off the boot critical path; embedding competes with inference.
      OnBootSec = "5min";
      OnUnitActiveSec = "1d";
      Unit = "witcher-corpus.service";
    };
  };

  # Plugins are database state too, so the same reconcile-on-activation
  # approach applies: the manifest is the desired state and the sync is a
  # no-op once it matches. Runs after the corpus so a single failure report
  # is about one thing at a time.
  systemd.services.openweb-ui-plugins = {
    description = "Sync Open WebUI plugins from the pinned manifest";
    after = ["open-webui.service" "witcher-corpus.service"];
    requires = ["open-webui.service"];
    wantedBy = ["multi-user.target"];
    serviceConfig = {
      Type = "oneshot";
      EnvironmentFile = config.age.secrets.open-webui-token.path;
      Restart = "on-failure";
      RestartSec = "5min";
    };
    script = ''
      exec ${openWebUi.sync}/bin/openweb-ui-sync ${
        openWebUi.manifest {
          usage_display =
            openWebUi.plugins.usage_display
            // {
              valves =
                openWebUi.plugins.usage_display.valves
                // {context_size_override = ctxSize;};
            };
        }
      }
    '';
  };

  # llama-bench/llama-cli on PATH for the tuning benchmark and debugging.
  environment.systemPackages = [llamaCppVulkan];
  # Model weights and wiki corpus live on the quota'd zpool/llm dataset.
  # World-readable: llama-swap runs as DynamicUser.
  systemd.tmpfiles.rules = [
    "d /var/lib/llm/models 0755 root root -"
    "d /var/lib/llm/corpus 0755 root root -"
    # Mounting a dataset under /var/lib/private creates the parent 0755;
    # systemd expects 0700 there and does not correct an existing directory.
    "d /var/lib/private 0700 root root -"
  ];

  # --- Family LLM service frontend (docs/local-llm-service.md, phase 2) ---
  # Reaches llama-swap over localhost; reachable itself only from the LAN
  # (t20's Caddy proxies it in phase 2.5). State dataset: hardware/sz1.nix.
  services.open-webui = {
    enable = true;
    # Bound to all interfaces, but the router never forwards this port and
    # the firewall opening below is the only path in.
    host = "0.0.0.0";
    port = 8080;
    # Would open the port on every interface, including the sect WireGuard
    # tunnel when it is up; scoped to the LAN NIC below instead.
    openFirewall = false;
    environment = {
      # Module defaults, restated because setting `environment` replaces them.
      SCARF_NO_ANALYTICS = "True";
      DO_NOT_TRACK = "True";
      ANONYMIZED_TELEMETRY = "False";
      # llama-swap speaks the OpenAI API; there is no ollama backend here.
      ENABLE_OLLAMA_API = "False";
      OPENAI_API_BASE_URL = "http://127.0.0.1:9292/v1";
      # llama-swap ignores the key, but the client refuses to send none.
      OPENAI_API_KEY = "sk-local";
      # Server-generated absolute links must use the public name. FritzBox
      # hairpinning resolves it from inside the LAN too, so one origin serves
      # everyone (D9) — this does not restrict which hostnames are accepted.
      WEBUI_URL = "https://chat.home.zabka.it";
      # Closed permanently: the admin panel creates users directly, so
      # self-registration is never needed — and this login is the public
      # perimeter once the t20 edge lands (D1).
      ENABLE_SIGNUP = "False";
      # Off by default upstream, which is what made the corpus reconciler's
      # key uncreatable. Note the plural: the singular name does nothing.
      # This is only a *default* — once the admin UI writes the matching
      # database row, that row wins and this stops having any effect.
      ENABLE_API_KEYS = "True";
    };
  };

  # The nix-serve half of this rule stays in hive.nix; list-valued options
  # merge across modules, so each subsystem opens its own port.
  networking.firewall.interfaces."enp4s0".allowedTCPPorts = [8080];
}
