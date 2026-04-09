{
  config,
  lib,
  pkgs,
  ...
}: let
  agentsFile = ./files/ai/AGENTS.md;
  claudeSettings =
    {
      hooks = {
        PreToolUse = [
          {
            matcher = "Bash";
            hooks = [
              {
                type = "command";
                command = "~/.claude/hooks/rtk-rewrite.sh";
              }
            ];
          }
        ];
        Stop = [
          {
            hooks = [
              {
                type = "command";
                command = "~/.claude/hooks/jj-describe-reminder.sh";
              }
            ];
          }
        ];
      };
    }
    // lib.optionalAttrs config.my.work.enable {
      mcpServers = {
        camunda-docs = {
          type = "http";
          url = "https://camunda-docs.mcp.kapa.ai";
        };
        context7 = {
          command = "npx";
          args = ["-y" "@upstash/context7-mcp"];
        };
      };
    };
  skillsDir = ./files/ai/skills;
  customAgentsDir = ./files/ai/agents;
  sources = import ../npins;
  crosslink = import ../apps/crosslink {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
  rtk = import ../apps/rtk {inherit pkgs sources;};
  claude-code = import ../apps/claude-code {inherit pkgs;};
  nucleus = sources.nucleus;
  mergedSkills = pkgs.runCommand "merged-skills" {} ''
    mkdir -p $out
    cp -r ${nucleus}/skills/. $out/
    chmod -R u+w $out
    cp -r ${skillsDir}/. $out/
  '';
  mergedAgents = pkgs.runCommand "merged-agents" {} ''
    mkdir -p $out
    cp -r ${nucleus}/agents/. $out/
    chmod -R u+w $out
    cp -r ${customAgentsDir}/. $out/
  '';
in {
  options.my.work.enable = lib.mkEnableOption "work machine configuration";

  home.sessionVariables = {
    CLAUDE_CONFIG_DIR = "\${XDG_CONFIG_HOME:-$HOME/.config}/claude";
    UV_PYTHON_PREFERENCE = "only-system";
    UV_PYTHON_PATH = "${pkgs.python3}/bin/python3";
  };

  programs.bash.initExtra = lib.mkAfter ''
    export CLAUDE_CONFIG_DIR="''${XDG_CONFIG_HOME:-$HOME/.config}/claude"
  '';

  programs.zsh.initContent = lib.mkAfter ''
    export CLAUDE_CONFIG_DIR="''${XDG_CONFIG_HOME:-$HOME/.config}/claude"
  '';

  xdg.configFile =
    lib.genAttrs [
      "opencode/AGENTS.md"
      "claude/CLAUDE.md"
    ] (_: {source = agentsFile;})
    // {
      "claude/skills".source = mergedSkills;
      "opencode/skills".source = mergedSkills;
      "claude/agents".source = mergedAgents;
    };

  home.packages = [crosslink cpitd rtk pkgs.jq pkgs.uv claude-code];

  home.file.".claude/hooks/rtk-rewrite.sh" = {
    source = ./files/ai/hooks/rtk-rewrite.sh;
    executable = true;
  };

  home.file.".claude/hooks/jj-describe-reminder.sh" = {
    source = ./files/ai/hooks/jj-describe-reminder.sh;
    executable = true;
  };

  # Symlink ~/.claude/skills -> ~/.config/claude/skills
  # CLAUDE_CONFIG_DIR doesn't fully support skill discovery
  home.file.".claude/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/skills";
  home.file.".claude/agents".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/agents";
  home.file.".claude/settings.json".text = builtins.toJSON claudeSettings;
}
