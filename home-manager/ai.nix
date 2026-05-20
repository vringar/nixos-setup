{
  config,
  lib,
  pkgs,
  ...
}: let
  agentsFile = ./files/ai/AGENTS.md;
  claudeSettings = {
    hooks = {
      PreToolUse = [
        {
          matcher = "Bash";
          hooks = [
            {
              type = "command";
              command = "~/.claude/hooks/rtk-rewrite.sh";
            }
            {
              type = "command";
              command = "~/.claude/hooks/gh-body-file-nudge.sh";
            }
            {
              type = "command";
              command = "~/.claude/hooks/jj-squash-stat.sh";
            }
          ];
        }
      ];
      SessionStart = [
        {
          matcher = "startup|clear";
          hooks = [
            {
              type = "command";
              command = "~/.claude/hooks/jj-dirty-wc-reminder.sh";
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
  };
  # Plugins to enable — format: "name@marketplace" (e.g. "typescript-lsp@claude-plugins-official").
  # The official marketplace is auto-downloaded by Claude on first run; declaring a plugin here
  # activates it without requiring a manual /plugin install.
  basePlugins = [];
  workPlugins = [];
  enabledPlugins = basePlugins ++ lib.optionals config.my.work.enable workPlugins;
  # settings.json expects a record: {"name@marketplace": true}, not an array
  enabledPluginsRecord = builtins.listToAttrs (
    map (id: {
      name = id;
      value = true;
    })
    enabledPlugins
  );

  # Hooks + plugins are merged into the CLAUDE_CONFIG_DIR settings.json at activation
  # time (see home.activation.claudeHooksSettings) rather than written as a
  # standalone file, because Claude Code reads $CLAUDE_CONFIG_DIR/settings.json
  # — not ~/.claude/settings.json — and also writes its own state into it.
  claudeHooksJson = pkgs.writeText "claude-hooks.json" (builtins.toJSON claudeSettings.hooks);
  claudePluginsJson = pkgs.writeText "claude-plugins.json" (builtins.toJSON enabledPluginsRecord);
  skillsDir = ./files/ai/skills;
  customAgentsDir = ./files/ai/agents;
  sources = import ../npins;
  crosslink = import ../apps/crosslink {inherit pkgs sources;};
  crossbridge = import ../apps/crossbridge {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
  rtk = import ../apps/rtk {inherit pkgs sources;};
  claude-sandbox = import ../apps/claude-sandbox {inherit pkgs;};
  element-templates-cli = import ../apps/element-templates-cli {inherit pkgs;};
  bpmnlint = import ../apps/bpmnlint {inherit pkgs sources;};
  nucleus = sources.nucleus;

  # Private repos — only forced when my.work.enable = true
  feel-mcp-server = import ../apps/feel-mcp-server {inherit pkgs sources;};
  camundaAiDevKit = sources.camunda-ai-dev-kit;

  workMcpServers = {
    camunda-docs = {
      transport = "http";
      commandOrUrl = "https://camunda-docs.mcp.kapa.ai";
      args = [];
    };
    context7 = {
      transport = "stdio";
      commandOrUrl = "npx";
      args = [
        "-y"
        "@upstash/context7-mcp"
      ];
    };
    feel-validator = {
      transport = "stdio";
      commandOrUrl = "${feel-mcp-server}/bin/feel-mcp-server";
      args = [];
    };
  };
  baseSkills = pkgs.runCommand "base-skills" {} ''
    mkdir -p $out
    cp -r ${nucleus}/skills/. $out/
    chmod -R u+w $out
    cp -r ${sources.crossbridge}/skill/. $out/
    chmod -R u+w $out
    cp -r ${skillsDir}/. $out/
  '';
  mergedSkills = pkgs.runCommand "merged-skills" {} ''
    mkdir -p $out
    cp -r ${nucleus}/skills/. $out/
    chmod -R u+w $out
    cp -r ${camundaAiDevKit}/.claude/skills/. $out/
    chmod -R u+w $out
    cp -r ${sources.crossbridge}/skill/. $out/
    chmod -R u+w $out
    cp -r ${skillsDir}/. $out/
  '';
  baseAgents = pkgs.runCommand "base-agents" {} ''
    mkdir -p $out
    cp -r ${nucleus}/agents/. $out/
    chmod -R u+w $out
    cp -r ${customAgentsDir}/. $out/
  '';
  mergedAgents = pkgs.runCommand "merged-agents" {} ''
    mkdir -p $out
    cp -r ${nucleus}/agents/. $out/
    chmod -R u+w $out
    cp -r ${camundaAiDevKit}/.claude/agents/. $out/
    chmod -R u+w $out
    cp -r ${customAgentsDir}/. $out/
  '';
in {
  imports = [./crossbridge-supervisor.nix];

  options.my.work.enable = lib.mkEnableOption "work machine configuration";

  config = {
    services.crossbridge-supervisor.enable = true;

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
      lib.genAttrs
      [
        "opencode/AGENTS.md"
        "claude/CLAUDE.md"
      ]
      (_: {
        source = agentsFile;
      })
      // (
        let
          skills =
            if config.my.work.enable
            then mergedSkills
            else baseSkills;
          agents =
            if config.my.work.enable
            then mergedAgents
            else baseAgents;
        in {
          "claude/skills".source = skills;
          "opencode/skills".source = skills;
          "claude/agents".source = agents;
        }
      );

    home.packages =
      [
        crosslink
        crossbridge
        cpitd
        rtk
        pkgs.jq
        pkgs.uv
        pkgs.claude-code
        claude-sandbox
      ]
      ++ lib.optionals config.my.work.enable [
        element-templates-cli
        bpmnlint
        feel-mcp-server
      ];

    home.file.".claude/hooks/rtk-rewrite.sh" = {
      source = ./files/ai/hooks/rtk-rewrite.sh;
      executable = true;
    };

    home.file.".claude/hooks/jj-describe-reminder.sh" = {
      source = ./files/ai/hooks/jj-describe-reminder.sh;
      executable = true;
    };

    home.file.".claude/hooks/jj-dirty-wc-reminder.sh" = {
      source = ./files/ai/hooks/jj-dirty-wc-reminder.sh;
      executable = true;
    };

    home.file.".claude/hooks/gh-body-file-nudge.sh" = {
      source = ./files/ai/hooks/gh-body-file-nudge.sh;
      executable = true;
    };

    home.file.".claude/hooks/jj-squash-stat.sh" = {
      source = ./files/ai/hooks/jj-squash-stat.sh;
      executable = true;
    };

    # Symlink ~/.claude/skills -> ~/.config/claude/skills
    # CLAUDE_CONFIG_DIR doesn't fully support skill discovery
    home.file.".claude/skills".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/skills";
    home.file.".claude/agents".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/claude/agents";

    # Merge our hooks into $CLAUDE_CONFIG_DIR/settings.json — the file Claude
    # Code actually reads. It is not written as a managed file because Claude
    # writes its own runtime state (enabledPlugins, effortLevel, ...) there;
    # jq merge preserves those keys while asserting our hooks block.
    home.activation.claudeHooksSettings = lib.hm.dag.entryAfter ["writeBoundary"] ''
      _settings="${config.xdg.configHome}/claude/settings.json"
      mkdir -p "$(dirname "$_settings")"
      if [ -e "$_settings" ]; then
        _merged=$(${pkgs.jq}/bin/jq \
          --slurpfile h ${claudeHooksJson} \
          --slurpfile p ${claudePluginsJson} \
          '.hooks = $h[0] | .enabledPlugins = $p[0]' "$_settings")
      else
        _merged=$(${pkgs.jq}/bin/jq -n \
          --slurpfile h ${claudeHooksJson} \
          --slurpfile p ${claudePluginsJson} \
          '{hooks: $h[0], enabledPlugins: $p[0]}')
      fi
      printf '%s\n' "$_merged" > "$_settings"
    '';

    # Register work MCP servers via `claude mcp add` so they appear in `claude mcp list`.
    # Uses home.activation to avoid clobbering Claude's own runtime state in .claude.json.
    home.activation.claudeMcpServers = lib.mkIf config.my.work.enable (
      lib.hm.dag.entryAfter ["writeBoundary"] (
        let
          claude = "${pkgs.claude-code}/bin/claude";
          addServer = name: cfg: let
            # Use -- separator when args contain flags (start with -) to prevent
            # claude mcp add from parsing them as its own options.
            hasFlags = builtins.any (a: lib.hasPrefix "-" a) cfg.args;
            sep = lib.optionalString hasFlags " --";
            extraArgs = lib.optionalString (cfg.args != []) " ${lib.concatStringsSep " " cfg.args}";
          in ''
            ${claude} mcp remove ${name} --scope user 2>/dev/null || true
            ${claude} mcp add --transport ${cfg.transport} --scope user ${name}${sep} ${cfg.commandOrUrl}${extraArgs}
          '';
        in
          lib.concatStrings (lib.mapAttrsToList addServer workMcpServers)
      )
    );
  }; # config
}
