# colmena config
let
  sources = import ./npins;
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
              "--temp 1.0"
              "--min-p 0.1"
            ];
            ttl = 1800;
          };
        };
      };
    };
    # llama-bench/llama-cli on PATH for the tuning benchmark and debugging.
    environment.systemPackages = [llamaCppVulkan];
    # Model weights and wiki corpus live on the quota'd zpool/llm dataset.
    # World-readable: llama-swap runs as DynamicUser.
    systemd.tmpfiles.rules = [
      "d /var/lib/llm/models 0755 root root -"
      "d /var/lib/llm/corpus 0755 root root -"
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
      ./modules/ghidra-server.nix
    ];

    services.ghidra-server = {
      enable = true;
      tailscaleCert = {
        enable = true;
        hostname = "t20.tailbaace.ts.net";
      };
    };
    services.tailscale.enable = true;

    boot.kernel.sysctl = {
      "net.ipv4.ip_forward" = 1;
      "net.ipv6.conf.all.forwarding" = 1;
    };

    security.sudo.wheelNeedsPassword = false;

    nix.settings.trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "sz1.fritz.box:CB1Zd3dpBNECfzeGVpkDNJYds4O/eKJhV2Tlx2NGqEc="
    ];

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
