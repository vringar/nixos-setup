{lib, ...}: let
  agentsFile = ./files/ai/AGENTS.md;
  skillsDir = ./files/ai/skills;
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
    };
}
