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
  basePlugins = ["jdtls-lsp@claude-plugins-official"];
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
  crosslink = import ../apps/crosslink {
    inherit pkgs sources;
    doCheck = config.my.crosslink.doCheck;
  };
  crossbridge = import ../apps/crossbridge {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
  rtk = import ../apps/rtk {inherit pkgs sources;};
  claude-sandbox = import ../apps/claude-sandbox {inherit pkgs;};
  bpmnlint = import ../apps/bpmnlint {inherit pkgs sources;};
  bpmn-auto-layout = import ../apps/bpmn-auto-layout {
    inherit pkgs sources;
    scriptSrc = "${skillsDir}/bpmn-generate/scripts/bpmn-auto-layout.cjs";
  };
  nucleus = sources.nucleus;

  # Private repos — only forced when my.work.enable = true
  feel-mcp-server = import ../apps/feel-mcp-server {inherit pkgs sources;};
  c8ctl-plugin-model = import ../apps/c8ctl-plugin-model {inherit pkgs sources;};
  dmnlint = import ../apps/dmnlint {inherit pkgs sources;};
  camundaSkills = sources.skills;

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
    cp -r ${camundaSkills}/skills/. $out/
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
in {
  imports = [./crossbridge-supervisor.nix];

  options.my.work.enable = lib.mkEnableOption "work machine configuration";

  options.my.crosslink.doCheck = lib.mkOption {
    type = lib.types.bool;
    default = true;
    description = "Run crosslink test suite during build. Disable on slow machines.";
  };

  config = {
    services.crossbridge-supervisor.enable = true;

    home.sessionVariables =
      {
        CLAUDE_CONFIG_DIR = "\${XDG_CONFIG_HOME:-$HOME/.config}/claude";
        UV_PYTHON_PREFERENCE = "only-system";
        UV_PYTHON_PATH = "${pkgs.python3}/bin/python3";
      }
      // lib.optionalAttrs config.my.work.enable {
        # Direct binary path for bpmnlint — avoids npx overhead (~370 ms → ~65 ms).
        # Consumed by BPMN skills via $BPMNLINT_BIN.
        BPMNLINT_BIN = "${bpmnlint}/bin/bpmnlint";
      };

    programs.bash.initExtra = lib.mkAfter ''
      export CLAUDE_CONFIG_DIR="''${XDG_CONFIG_HOME:-$HOME/.config}/claude"
    '';

    programs.zsh.initContent = lib.mkAfter ''
      export CLAUDE_CONFIG_DIR="''${XDG_CONFIG_HOME:-$HOME/.config}/claude"
    '';

    programs.bash.shellAliases = {xl = "crosslink";};
    programs.zsh.shellAliases = {xl = "crosslink";};

    # crossbridge ships a direnv helper exposing the `crossbridge_up`
    # function; loading it into the user's direnvrc lets any crosslink
    # repo's .envrc bootstrap a per-repo server with a single line.
    programs.direnv.stdlib = builtins.readFile "${sources.crossbridge}/nix/direnvrc.sh";

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
          agents = baseAgents;
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
        pkgs.jdt-language-server
        claude-sandbox
      ]
      ++ lib.optionals config.my.work.enable [
        bpmnlint
        bpmn-auto-layout
        dmnlint
        feel-mcp-server
        c8ctl-plugin-model
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
    # Register c8ctl-plugin-model in the c8ctl global plugin registry.
    # Plugin dir: ~/.config/c8ctl/plugins/node_modules/
    # Registry:   ~/.config/c8ctl/plugins.json
    home.activation.c8ctlPlugins = lib.mkIf config.my.work.enable (
      lib.hm.dag.entryAfter ["writeBoundary"] ''
        _c8ctl_plugins_dir="''${XDG_CONFIG_HOME:-$HOME/.config}/c8ctl/plugins/node_modules"
        _c8ctl_plugins_json="''${XDG_CONFIG_HOME:-$HOME/.config}/c8ctl/plugins.json"

        mkdir -p "$_c8ctl_plugins_dir"

        rm -f "$_c8ctl_plugins_dir/c8ctl-plugin-model"
        ln -s "${c8ctl-plugin-model}/lib/node_modules/c8ctl-plugin-model" \
          "$_c8ctl_plugins_dir/c8ctl-plugin-model"

        if [ -f "$_c8ctl_plugins_json" ]; then
          _existing=$(cat "$_c8ctl_plugins_json")
        else
          _existing='{"plugins":[]}'
        fi
        printf '%s' "$_existing" \
          | ${pkgs.jq}/bin/jq \
            --arg src "file://${c8ctl-plugin-model}/lib/node_modules/c8ctl-plugin-model" \
            '(.plugins // []) |= map(select(.name != "c8ctl-plugin-model"))
             | .plugins += [{"name":"c8ctl-plugin-model","source":$src,"installedAt":"1970-01-01T00:00:00.000Z"}]' \
          > "$_c8ctl_plugins_json"
      ''
    );
  }; # config
}
