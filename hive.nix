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
      {home-manager.users.vringar = import ./home-manager/ghidra.nix;}
      {home-manager.users.vringar = import ./home-manager/zellij-resilient.nix;}
      {home-manager.users.vringar = import ./home-manager/claude-sandbox.nix;}
    ];
    nix.settings.secret-key-files = ["/etc/nix/signing-key.sec"];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";

    system.stateVersion = "25.05";
  };

  t20 = {lib, ...}: {
    imports = [
      ./hardware/pi.nix
      ./modules/ghidra-server.nix
    ];

    # Headless server — server shell config only, no desktop packages
    home-manager.users = lib.mkForce {
      vringar = import ./home-manager/server.nix;
    };
    users.users.vringar.packages = lib.mkForce [];

    services.ghidra-server.enable = true;
    services.tailscale.enable = true;

    security.sudo.wheelNeedsPassword = false;

    nix.settings.trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "sz1.fritz.box:CB1Zd3dpBNECfzeGVpkDNJYds4O/eKJhV2Tlx2NGqEc="
    ];

    deployment.tags = ["personal"];
    deployment.targetHost = "t20.fritz.box";
    deployment.targetUser = "vringar";
    deployment.buildOnTarget = true; # bootstrap only: remove after first successful deploy

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

    virtualisation.docker = {
      enable = true;
      storageDriver = "btrfs";
    };
    system.stateVersion = "24.11";
  };
}
