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

    deployment.replaceUnknownProfiles = true;
    nixpkgs.flake.source = sources.nixpkgs;

    # Pin system-wide nixpkgs to npins
    nix.nixPath = ["nixpkgs=${sources.nixpkgs}"];
    nix.channel.enable = false;
  };

  sz1 = {...}: {
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      ./hardware/sz1.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./modules/wg-sect.nix
      {home-manager.users.vringar = import ./home-manager/ai.nix;}
      {home-manager.users.vringar = import ./home-manager/ghidra.nix;}
      {home-manager.users.vringar = import ./home-manager/zellij-resilient.nix;}
      {home-manager.users.vringar = import ./home-manager/claude-sandbox.nix;}
    ];
    nix.settings.secret-key-files = ["/etc/nix/signing-key.sec"];

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
      {home-manager.users.vringar = import ./home-manager/ai.nix;}
    ];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";

    virtualisation.docker = {
      enable = true;
      storageDriver = "btrfs";
    };
    system.stateVersion = "24.11";
  };
}
