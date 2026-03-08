{
  config,
  lib,
  pkgs,
  ...
}: let
  agentsFile = ./files/ai/AGENTS.md;
  skillsDir = ./files/ai/skills;
  customAgentsDir = ./files/ai/agents;
  sources = import ../npins;
  crosslink = import ../apps/crosslink {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
  rtk = import ../apps/rtk {inherit pkgs sources;};
in {
  home.sessionVariables = {
    CLAUDE_CONFIG_DIR = "\${XDG_CONFIG_HOME:-$HOME/.config}/claude";
    UV_PYTHON_PREFERENCE = "only-system";
    UV_PYTHON_PATH = "${pkgs.python3}/bin/python3";
  };

  xdg.configFile =
    lib.genAttrs [
      "opencode/AGENTS.md"
      "claude/CLAUDE.md"
    ] (_: {source = agentsFile;})
    // {
      "claude/skills".source = skillsDir;
      "opencode/skills".source = skillsDir;
      "claude/agents".source = customAgentsDir;
      "opencode/agents".source = customAgentsDir;
    };

  home.packages = [crosslink cpitd rtk pkgs.jq pkgs.uv];

  home.file.".claude/hooks/rtk-rewrite.sh" = {
    source = ./files/ai/hooks/rtk-rewrite.sh;
    executable = true;
  };

  # Symlink ~/.claude/skills -> ~/.config/claude/skills
  # CLAUDE_CONFIG_DIR doesn't fully support skill discovery
  home.file.".claude/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/skills";
  home.file.".claude/agents".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/agents";
  home.file.".claude/settings.json".source = ./files/ai/claude-settings.json;
}
