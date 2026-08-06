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
    nixpkgs = sources.nixpkgs;
  };

  defaults = {...}: {
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
    ...
  }: let
    llamaCppVulkan = pkgs.llama-cpp.override {vulkanSupport = true;};
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
          "--ctx-size 12288"
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

  t20 = {
    pkgs,
    lib,
    config,
    ...
  }: {
    imports = [
      ./hardware/pi.nix
    ];

    services.tailscale.enable = true;

    # --- Public HTTPS edge (docs/local-llm-service.md, phase 2.5) ---
    # Only t20:443 faces the internet; it terminates TLS and proxies to Open
    # WebUI on sz1, which stays LAN-bound. The cert is a DNS-01 *wildcard* so
    # `chat` never appears in Certificate Transparency logs (D9), which needs
    # the INWX solver — stock Caddy can only do HTTP-01, and HTTP-01 cannot
    # issue a wildcard.
    services.caddy = {
      enable = true;
      package = pkgs.caddy.withPlugins {
        plugins = ["github.com/caddy-dns/inwx@v0.4.1"];
        hash = "sha256-jhZRekdz/aWA44mNIxwfLPbM/BYjYcLMFEsnbVVKxlQ=";
      };
      # ONE site block for the whole wildcard, services dispatched by host
      # matcher inside it. A named vhost (chat.home.zabka.it) would make Caddy
      # issue a *named* cert for it — straight into the CT logs, defeating the
      # point of the wildcard. Adding a service later = new handle block, no
      # ACME round trip.
      virtualHosts."*.home.zabka.it".extraConfig = ''
        tls {
          dns inwx {
            username {env.INWX_USER}
            password {env.INWX_PASSWORD}
          }
        }

        @chat host chat.home.zabka.it
        handle @chat {
          # sz1 stays on plain HTTP behind the LAN firewall; SSE streaming
          # from llama-server passes through reverse_proxy without extra
          # buffering configuration.
          reverse_proxy http://sz1.fritz.box:8080
        }

        # Unknown *.home.zabka.it names: don't reveal what exists.
        handle {
          respond "" 404
        }

        # sz1 unreachable (D14): serve the diagnosis page rendered by
        # edge-status.timer instead of a bare 502.
        handle_errors {
          root * /run/edge-status
          rewrite * /status.html
          file_server
        }
      '';
    };

    # D14: when sz1 is unreachable, tell the family member *why*. A timer
    # probes sz1 each minute and renders the page handle_errors serves:
    # port open → transient; ping but no port → booted into Windows;
    # no ping → powered off.
    systemd.services.edge-status = {
      description = "Render sz1 reachability status page for the Caddy edge";
      serviceConfig.Type = "oneshot";
      script = ''
        if ${pkgs.coreutils}/bin/timeout 2 ${pkgs.bash}/bin/bash \
          -c 'exec 3<>/dev/tcp/sz1.fritz.box/8080' 2>/dev/null; then
          msg="sz1 is reachable — this page should be gone in a moment. Retry."
        elif ${pkgs.iputils}/bin/ping -c1 -W2 sz1.fritz.box >/dev/null 2>&1; then
          msg="sz1 is currently booted into Windows. Chat is unavailable until it is back in Linux."
        else
          msg="sz1 is powered off. Ask Stefan to switch it on."
        fi
        printf '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>chat status</title></head><body style="font-family:sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem"><h1>Chat is taking a break</h1><p>%s</p></body></html>' "$msg" \
          > /run/edge-status/status.html.tmp
        mv /run/edge-status/status.html.tmp /run/edge-status/status.html
      '';
    };
    systemd.timers.edge-status = {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnBootSec = "30s";
        OnUnitActiveSec = "60s";
      };
    };
    systemd.tmpfiles.rules = ["d /run/edge-status 0755 root root -"];

    # Open WebUI's login is the public perimeter (D1). Credentials live in an
    # environment file rather than the Nix store, which is world-readable.
    age.secrets.inwx.file = ./secrets/inwx.age;
    systemd.services.caddy.serviceConfig.EnvironmentFile = config.age.secrets.inwx.path;

    networking.firewall.allowedTCPPorts = [80 443];

    # Caddy signals readiness itself (Type=notify), so this only has to catch
    # the cases where it comes up serving nothing. The admin API is local and
    # always present; certificate issuance happens afterwards and is not
    # gated here — a wildcard that fails DNS-01 still shows up in the journal.
    systemd.services.caddy.serviceConfig.ExecStartPost = httpReady {
      inherit pkgs;
      url = "http://127.0.0.1:2019/config/";
    };

    # A Caddyfile that nix evaluates happily can still be rejected by the
    # config adapter, which only runs when Caddy starts — a green build then
    # deploys an edge that never comes up. Adapting the generated config at
    # build time moves that failure left; it exercises plugin directives too,
    # since the check runs the same caddy build the host will.
    system.checks = [
      (pkgs.runCommand "caddy-config-adapts" {} ''
        ${lib.getExe config.services.caddy.package} adapt \
          --config ${config.services.caddy.configFile} \
          --adapter caddyfile >/dev/null
        touch $out
      '')
    ];

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
