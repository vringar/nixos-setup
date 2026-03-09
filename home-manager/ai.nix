{
  config,
  lib,
  pkgs,
  ...
}: let
  agentsFile = ./files/ai/AGENTS.md;
  skillsDir = ./files/ai/skills;
  sources = import ../npins;
  crosslink = import ../apps/crosslink {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
in {
  home.sessionVariables = {
    CLAUDE_CONFIG_DIR = "\${XDG_CONFIG_HOME:-$HOME/.config}/claude";
  };

  xdg.configFile =
    lib.genAttrs [
      "opencode/AGENTS.md"
      "claude/CLAUDE.md"
    ] (_: {source = agentsFile;})
    // {
      "claude/skills".source = skillsDir;
      "opencode/skills".source = skillsDir;
    };

  home.packages = [crosslink cpitd];

  # Symlink ~/.claude/skills -> ~/.config/claude/skills
  # CLAUDE_CONFIG_DIR doesn't fully support skill discovery
  home.file.".claude/skills".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/skills";
}
