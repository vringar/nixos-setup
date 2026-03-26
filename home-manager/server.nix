# Minimal home-manager config for headless servers.
# Shares git/ssh/shell setup with baseline but drops desktop packages.
{
  pkgs,
  config,
  lib,
  ...
}: let
  cfg = config.my.user;
  hasIdentity = cfg.name != null && cfg.email != null;
in {
  options.my.user = {
    name = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
    };
    email = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
    };
    sshKeyName = lib.mkOption {
      type = lib.types.str;
      default = "github_key";
    };
  };

  config = {
    home.packages = [
      pkgs.gh
      pkgs.git-lfs
      pkgs.htop
      pkgs.jujutsu
    ];

    programs.neovim = {
      enable = true;
      defaultEditor = true;
      viAlias = true;
      vimAlias = true;
    };

    programs.bash = {
      enable = true;
    };

    programs.zsh = {
      enable = true;
      oh-my-zsh = {
        enable = true;
        plugins = ["git" "nix-shell"];
      };
    };
    home.shell.enableZshIntegration = true;

    programs.starship.enable = true;

    programs.git = {
      package = pkgs.gitFull;
      enable = true;
      lfs.enable = true;
      settings.user.name = lib.mkIf hasIdentity cfg.name;
      settings.user.email = lib.mkIf hasIdentity cfg.email;
      signing = {
        signByDefault = true;
        format = "ssh";
        key = "/home/${config.home.username}/.ssh/${cfg.sshKeyName}.pub";
      };
      settings = {
        core.editor = lib.getExe config.programs.neovim.finalPackage;
        rerere.enable = true;
        fetch.prune = true;
        init.defaultBranch = "main";
        pull.ff = "only";
        push = {
          default = "upstream";
          autoSetupRemote = true;
        };
      };
    };

    programs.jujutsu = {
      enable = true;
      settings =
        {}
        // lib.optionalAttrs hasIdentity {
          user = {
            name = cfg.name;
            email = cfg.email;
          };
        };
    };

    services.ssh-agent.enable = true;

    programs.ssh = {
      enable = true;
      enableDefaultConfig = false;
      extraOptionOverrides = {
        IgnoreUnknown = "GSSAPIKexAlgorithms,GSSAPIAuthentication";
        IdentityFile = "~/.ssh/${cfg.sshKeyName}";
        AddKeysToAgent = "yes";
      };
      matchBlocks = {
        "jannis-gogs" = {
          hostname = "omniskop.de";
          user = "gogs";
        };
        "hg.mozilla.org" = {
          user = "szabka@mozilla.com";
          identityFile = "~/.ssh/mozilla";
        };
      };
    };

    home.activation.fixSSHConfigPermissions = lib.hm.dag.entryAfter ["linkGeneration"] ''
      sshConfig="${config.home.homeDirectory}/.ssh/config"
      if [ -L "$sshConfig" ]; then
        realSource=$(readlink -f "$sshConfig")
        rm "$sshConfig"
        cp "$realSource" "$sshConfig"
        chmod 600 "$sshConfig"
      fi
    '';

    home.stateVersion = "25.11";
  };
}
