# colmena config
let
  sources = import ./npins;
in {
  meta = {
    # Override to pin the Nixpkgs version (recommended). This option
    # accepts one of the following:
    # - A path to a Nixpkgs checkout
    # - The Nixpkgs lambda (e.g., import <nixpkgs>)
    # - An initialized Nixpkgs attribute set
    nixpkgs = sources.nixpkgs;

    # If your Colmena host has nix configured to allow for remote builds
    # (for nix-daemon, your user being included in trusted-users)
    # you can set a machines file that will be passed to the underlying
    # nix-store command during derivation realization as a builders option.
    # For example, if you support multiple orginizations each with their own
    # build machine(s) you can ensure that builds only take place on your
    # local machine and/or the machines specified in this file.
    # machinesFile = ./machines.client-a;
  };

  defaults = {pkgs, ...}: {
    nixpkgs.config.allowUnfree = true;

    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      (import "${sources.home-manager}/nixos")
      ./modules/baseline.nix
    ];

    # By default, Colmena will replace unknown remote profile
    # (unknown means the profile isn't in the nix store on the
    # host running Colmena) during apply (with the default goal,
    # boot, and switch).
    # If you share a hive with others, or use multiple machines,
    # and are not careful to always commit/push/pull changes
    # you can accidentaly overwrite a remote profile so in those
    # scenarios you might want to change this default to false.
    deployment.replaceUnknownProfiles = true;
    # Enable networking
    networking.networkmanager.enable = true;

    # Set your time zone.
    time.timeZone = "Europe/Berlin";

    # Select internationalisation properties.
    i18n.defaultLocale = "en_GB.UTF-8";

    i18n.extraLocaleSettings = {
      LC_ADDRESS = "de_DE.UTF-8";
      LC_IDENTIFICATION = "de_DE.UTF-8";
      LC_MEASUREMENT = "de_DE.UTF-8";
      LC_MONETARY = "de_DE.UTF-8";
      LC_NAME = "de_DE.UTF-8";
      LC_NUMERIC = "de_DE.UTF-8";
      LC_PAPER = "de_DE.UTF-8";
      LC_TELEPHONE = "de_DE.UTF-8";
      LC_TIME = "de_DE.UTF-8";
    };
    # We need the flakes experimental feature to do the NIX_PATH thing cleanly
    # below. Given that this is literally the default config for flake-based
    # NixOS installations in the upcoming NixOS 24.05, future Nix/Lix releases
    # will not get away with breaking it.
    nix.settings = {
      experimental-features = "nix-command flakes";
    };
    nixpkgs.flake.source = sources.nixpkgs;
    services.openssh.enable = true;

    users.users.vringar = import ./user {pkgs = pkgs;};
  };

  sz1 = {
    name,
    lib,
    pkgs,
    ...
  }: {
    imports = [
      ./hardware/sz1.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
    ];
    deployment.tags = ["personal"];

    deployment.allowLocalDeployment = true;

    system.stateVersion = "25.05";
  };

  sz3 = {
    name,
    lib,
    pkgs,
    ...
  }: {
    imports = [
      ./hardware/sz3.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
    ];
    networking.hostName = name;
    # Bootloader.
    boot.loader.systemd-boot = {
      enable = true;
      configurationLimit = 10;
    };
    boot.loader.efi.canTouchEfiVariables = true;

    # You can filter hosts by tags with --on @tag-a,@tag-b.
    # In this example, you can deploy to hosts with the "web" tag using:
    #    colmena apply --on @web
    # You can use globs in tag matching as well:
    #    colmena apply --on '@infra-*'
    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;

    virtualisation.docker = {
      enable = true;
      storageDriver = "btrfs";
    };
    system.stateVersion = "24.11";
  };
}
