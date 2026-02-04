{pkgs, ...}: {
  imports = [
    ../home-manager/baseline.nix
  ];

  nixpkgs.config.allowUnfree = true;

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
    LC_TIME = "sv_SE.UTF-8"; # Swedish dates
  };

  # Nix settings
  nix.settings = {
    experimental-features = "nix-command flakes";
  };

  # SSH server
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
    };
  };

  # Allow passwordless sudo for colmena remote deployment
  # See: https://github.com/zhaofengli/colmena/blob/main/src/nix/host/ssh.rs
  security.sudo.extraRules = [
    {
      users = ["vringar"];
      commands = [
        {
          command = "/run/current-system/sw/bin/nix-env --profile /nix/var/nix/profiles/system --set /nix/store/*";
          options = ["NOPASSWD"];
        }
        {
          command = "/nix/store/*/bin/switch-to-configuration *";
          options = ["NOPASSWD"];
        }
      ];
    }
  ];

  services.tailscale.enable = true;

  # System packages
  environment.systemPackages = with pkgs; [
    vim
    wget
    curl
    npins
    lixPackageSets.git.colmena
    git
    git-lfs
    cifs-utils
    jujutsu
    pkgs.home-manager
  ];

  home-manager.useGlobalPkgs = true;
  programs.zsh.ohMyZsh.enable = true;
  programs.zsh.enable = true;

  # User configuration
  users.users.vringar =
    import ../user {pkgs = pkgs;}
    // {
      openssh.authorizedKeys.keyFiles = [
        ../home-manager/files/ssh/github_key.pub
      ];
    };

  my.username = "vringar";
}
