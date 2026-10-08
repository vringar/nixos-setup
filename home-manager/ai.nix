{
  config,
  lib,
  pkgs,
  ...
}: let
  agentsFile = ./files/ai/AGENTS.md;
  claudeSettings = {
    # Transcripts under $CLAUDE_CONFIG_DIR/projects are the corpus for
    # claude-recall. Claude Code's default sweep had already deleted months
    # of history — eleven project directories were left holding only a
    # memory/ subdir, and nothing survived between 2026-02-02 and
    # 2026-07-21. Keep them; claude-recall archives out of band as well, so
    # a regression in either mechanism alone is not lossy.
    cleanupPeriodDays = 36500;

    outputStyle = "Concise";

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
              command = "~/.claude/hooks/gh-issue-template-guard.py";
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
  basePlugins = [
    "jdtls-lsp@claude-plugins-official"
    "rust-analyzer-lsp@claude-plugins-official"
  ];
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
  claudeCleanupDays = builtins.toString claudeSettings.cleanupPeriodDays;
  claudeOutputStyle = claudeSettings.outputStyle;
  skillsDir = ./files/ai/skills;
  workSkillsDir = ./files/ai/skills-work;
  customAgentsDir = ./files/ai/agents;
  sources = import ../npins;
  crosslink = import ../apps/crosslink {
    inherit pkgs sources;
    doCheck = config.my.crosslink.doCheck;
  };
  crossbridge = import ../apps/crossbridge {inherit pkgs sources;};
  cpitd = import ../apps/crosslink/cpitd.nix {inherit pkgs sources;};
  rtk = import ../apps/rtk {inherit pkgs sources;};
  # Track Anthropic's release manifest directly rather than waiting on nixpkgs.
  claude-code = pkgs.claude-code.override {
    manifest = lib.importJSON ../apps/claude-code/manifest.zst.json;
  };
  claude-sandbox = import ../apps/claude-sandbox {inherit pkgs;};
  claude-recall = import ../apps/claude-recall {inherit pkgs;};
  message-board = import ../apps/message-board {inherit pkgs sources;};
  bpmnlint = import ../apps/bpmnlint {inherit pkgs sources;};
  bpmn-auto-layout = import ../apps/bpmn-auto-layout {
    inherit pkgs sources;
    scriptSrc = "${skillsDir}/bpmn-generate/scripts/bpmn-auto-layout.cjs";
  };
  bpmn-to-image = import ../apps/bpmn-to-image {inherit pkgs sources;};
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
    chmod -R u+w $out
    cp -r ${workSkillsDir}/. $out/
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
        # Direct binary path for bpmn-to-image — avoids the npx first-run
        # package fetch and puppeteer's own Chromium download (Chromium and
        # PUPPETEER_EXECUTABLE_PATH are baked into the wrapper already).
        # Consumed by the bpmn-render skill via $BPMN_TO_IMAGE_BIN.
        BPMN_TO_IMAGE_BIN = "${bpmn-to-image}/bin/bpmn-to-image";
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
        claude-code
        pkgs.jdt-language-server
        pkgs.rust-analyzer
        claude-sandbox
        claude-recall
        message-board
      ]
      ++ lib.optionals config.my.work.enable [
        bpmnlint
        bpmn-auto-layout
        bpmn-to-image
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

    home.file.".claude/hooks/gh-issue-template-guard.py" = {
      source = ./files/ai/hooks/gh-issue-template-guard.py;
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
          --argjson c ${claudeCleanupDays} \
          --arg o ${lib.escapeShellArg claudeOutputStyle} \
          '.hooks = $h[0] | .enabledPlugins = $p[0] | .cleanupPeriodDays = $c | .outputStyle = $o' "$_settings")
      else
        _merged=$(${pkgs.jq}/bin/jq -n \
          --slurpfile h ${claudeHooksJson} \
          --slurpfile p ${claudePluginsJson} \
          --argjson c ${claudeCleanupDays} \
          --arg o ${lib.escapeShellArg claudeOutputStyle} \
          '{hooks: $h[0], enabledPlugins: $p[0], cleanupPeriodDays: $c, outputStyle: $o}')
      fi
      printf '%s\n' "$_merged" > "$_settings"
    '';

    # Local message board for coordinating multi-repo agent work. Long-lived
    # user service; binds 127.0.0.1 only (no auth yet). The DB lives in the
    # user state dir so it survives restarts and re-derives the board.
    systemd.user.services.message-board = {
      Unit = {
        Description = "Local long-poll message board for agent coordination";
      };
      Install = {
        WantedBy = ["default.target"];
      };
      Service = {
        Type = "simple";
        ExecStart = "${message-board}/bin/board serve";
        Restart = "on-failure";
        RestartSec = "5s";
        StateDirectory = "message-board";
        Environment = ["BOARD_DB=%S/message-board/board.db"];
      };
    };

    # Nightly transcript archive + index rebuild for `claude-recall`.
    #
    # Two units rather than one so the archive — the step that protects
    # against transcript loss — still runs if indexing fails. Both are
    # short-lived batch jobs; nothing stays resident. Peak RSS is ~460 MB
    # during embedding, which is why the unit pins ONNX Runtime's thread
    # count (see apps/claude-recall/claude_recall.py).
    systemd.user.services.claude-recall-index = {
      Unit = {
        Description = "Archive Claude Code transcripts and rebuild the recall index";
      };
      Service = {
        Type = "oneshot";
        Nice = 15;
        IOSchedulingClass = "idle";
        Environment = ["CLAUDE_RECALL_THREADS=2" "CLAUDE_RECALL_BATCH=8"];
        ExecStart = [
          "${claude-recall}/bin/claude-recall archive"
          "${claude-recall}/bin/claude-recall index"
        ];
      };
    };

    systemd.user.timers.claude-recall-index = {
      Unit = {
        Description = "Nightly claude-recall archive and index";
      };
      Timer = {
        OnCalendar = "daily";
        # The machine is not always up at the scheduled time; catch up on
        # the next boot rather than silently skipping a day of transcripts.
        Persistent = true;
        RandomizedDelaySec = "30m";
      };
      Install = {
        WantedBy = ["timers.target"];
      };
    };

    # Register work MCP servers via `claude mcp add` so they appear in `claude mcp list`.
    # Uses home.activation to avoid clobbering Claude's own runtime state in .claude.json.
    home.activation.claudeMcpServers = lib.mkIf config.my.work.enable (
      lib.hm.dag.entryAfter ["writeBoundary"] (
        let
          claude = "${claude-code}/bin/claude";
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
