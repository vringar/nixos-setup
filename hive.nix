# colmena config
let
  sources = import ./npins;
in {
  meta = {
    nixpkgs = sources.nixpkgs;
  };

  defaults = {...}: {
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      (import "${sources.home-manager}/nixos")
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
      ./hardware/sz1.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./home-manager/ghidra.nix
      ./home-manager/zellij-resilient.nix
      ./home-manager/claude-sandbox.nix
    ];
    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";

    system.stateVersion = "25.05";
  };

  sz3 = {...}: {
    imports = [
      ./hardware/sz3.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
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
