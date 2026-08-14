# colmena config
let
  sources = import ./npins;

  # systemd has no readiness probe: a unit counts as started the moment its
  # process forks, so a server that accepts connections and then answers
  # nothing looks perfectly healthy — which is how a nix-serve whose workers
  # all aborted stayed "active (running)" while t20 timed out against it.
  # ExecStartPost failing marks the unit failed, so `colmena apply` reports a
  # service that came up but does not serve, instead of leaving it for the
  # first user to discover. Liveness only: it says the endpoint answers, not
  # that the service is correct.
  httpReady = {
    pkgs,
    url,
    retries ? 10,
  }:
    "${pkgs.curl}/bin/curl --fail --silent --show-error --output /dev/null"
    + " --connect-timeout 2 --max-time 5"
    + " --retry ${toString retries} --retry-delay 2 --retry-all-errors ${url}";
in {
  meta = {
    nixpkgs = import sources.nixpkgs {config.allowUnfree = true;};
  };

  defaults = {...}: {
    # Available to every host module, not just the per-host blocks below.
    _module.args.httpReady = httpReady;

    imports = [
      (import "${sources.home-manager}/nixos")
      (import "${sources.agenix}/modules/age.nix")
      ./modules/baseline.nix
    ];

    home-manager.sharedModules = [
      (import "${sources.plasma-manager}/modules")
    ];

    deployment.replaceUnknownProfiles = true;
    nixpkgs.flake.source = sources.nixpkgs;

    # Pin system-wide nixpkgs to npins
    nix.nixPath = ["nixpkgs=${sources.nixpkgs}"];
    nix.channel.enable = false;
  };

  sz1 = {
    pkgs,
    lib,
    config,
    ...
  }: let
    llamaCppVulkan = pkgs.llama-cpp.override {vulkanSupport = true;};
    # One source of truth: the server is started with this, and the usage
    # plugin is told the same number, so the context meter cannot drift from
    # the context actually served.
    ctxSize = 12288;
    parallelSlots = 2;
    openWebUi = pkgs.callPackage ./apps/openweb-ui {};
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
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      ./hardware/sz1.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./modules/wg-sect.nix
      {home-manager.users.vringar = import ./home-manager/ghidra.nix;}
      {home-manager.users.vringar = import ./home-manager/zellij-resilient.nix;}
    ];
    nix.settings.secret-key-files = ["/etc/nix/signing-key.sec"];

    # Serve sz1's store as a binary cache so t20 can *substitute* its aarch64
    # closure instead of depending on colmena pushing every path. Reuses the
    # signing key sz1 already has and the public key t20 already trusts (see
    # scripts/sign-for-t20.sh), so no new key material is involved.
    services.nix-serve = {
      enable = true;
      # The default nix-serve is a Perl app needing Nix's Perl bindings, which
      # Lix does not ship: workers die on `Can't locate Nix/Config.pm`, so the
      # listening socket accepts connections that are never served and clients
      # hang until they time out. nix-serve-ng is a drop-in with no Perl.
      package = pkgs.nix-serve-ng;
      secretKeyFile = "/etc/nix/signing-key.sec";
      # openFirewall would open the port on every interface, including the
      # wg-sect tunnel; scoped to the LAN NIC with Open WebUI's rule instead.
      openFirewall = false;
    };

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
    # Answering /nix-cache-info is the whole job of this service, and the one
    # thing a broken Perl build never managed.
    systemd.services.nix-serve.serviceConfig.ExecStartPost = httpReady {
      inherit pkgs;
      url = "http://127.0.0.1:5000/nix-cache-info";
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
    age.secrets.open-webui-token.file = ./secrets/open-webui-token.age;
    systemd.services.witcher-corpus = {
      description = "Reconcile the Witcher lore corpus into Open WebUI";
      # open-webui only counts as started once /health answers (see its
      # ExecStartPost), so ordering after it is meaningful rather than a race.
      after = ["open-webui.service" "network-online.target"];
      wants = ["network-online.target"];
      requires = ["open-webui.service"];
      wantedBy = ["multi-user.target"];
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
          ${./apps/witcher-corpus/reconcile.py} \
          witcher-lore ${pkgs.callPackage ./apps/witcher-corpus {}}
      '';
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
      };
    };
    # LAN NIC only: t20's Caddy and household clients, never the wg-sect tunnel.
    # enp4s0 is a PCI-path name, stable unless the NIC is replaced or moved —
    # if it ever changes, this rule stops matching and the UI goes unreachable
    # from the LAN rather than becoming over-exposed.
    networking.firewall.interfaces."enp4s0".allowedTCPPorts = [
      8080 # Open WebUI
      5000 # nix-serve binary cache
    ];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";
    services.teamviewer.enable = true;
    system.stateVersion = "25.05";
  };

  t20 = {...}: {
    imports = [
      ./hardware/pi.nix
      ./t20/caddy.nix
    ];

    services.tailscale.enable = true;

    # Stable-privacy SLAAC derives the interface ID from the prefix, so a
    # prefix change (reconnect, move) would break the FritzBox IPv6 exposure
    # rule and the AAAA record. Pin the ID instead: t20 is always <prefix>::443.
    # Same UUID as the auto-generated profile so this replaces it.
    networking.networkmanager.ensureProfiles.profiles.wired = {
      connection = {
        id = "Wired connection 1";
        uuid = "fe873ff9-4ca7-309e-bbf9-5dc1fe85e60f";
        type = "ethernet";
        interface-name = "enu1u1u1";
      };
      ipv4.method = "auto";
      ipv6 = {
        method = "auto";
        addr-gen-mode = "eui64";
        token = "::443";
      };
    };

    boot.kernel.sysctl = {
      "net.ipv4.ip_forward" = 1;
      "net.ipv6.conf.all.forwarding" = 1;
    };

    security.sudo.wheelNeedsPassword = false;

    # The sz1 key authorizes signatures on paths colmena pushes after
    # scripts/sign-for-t20.sh signs them — it is load-bearing for deploys, not
    # only for the substituter below.
    nix.settings.trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "sz1.fritz.box:CB1Zd3dpBNECfzeGVpkDNJYds4O/eKJhV2Tlx2NGqEc="
    ];

    # Pull from sz1 first: it has already built this host's closure, and the Pi
    # cannot realistically build anything itself. `extra-` so cache.nixos.org
    # stays in the list. An unreachable substituter is a warning, not an error,
    # so t20 still deploys when sz1 is off or in Windows.
    nix.settings.extra-substituters = ["http://sz1.fritz.box:5000"];

    deployment.tags = ["personal"];
    deployment.targetHost = "t20.fritz.box";
    deployment.targetUser = "vringar";

    system.stateVersion = "25.05";
  };

  sz3 = {...}: {
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      ./hardware/sz3.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./modules/wg-sect.nix
    ];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";

    users.users.vringar.extraGroups = ["docker"];
    virtualisation.docker = {
      enable = true;
      storageDriver = "btrfs";
    };
    system.stateVersion = "24.11";
  };
}
