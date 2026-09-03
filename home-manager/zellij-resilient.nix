{
  lib,
  pkgs,
  ...
}: {
  # systemd-oomd measures memory pressure on a cgroup marked kill, then kills
  # one of its DIRECT children. Konsole marks its own scope, and the whole
  # multiplexer -- server plus every pane -- lives in a single Konsole tab
  # scope, so one runaway pane takes every project down with it. Marking this
  # slice makes each pane a candidate in its own right.
  #
  # Deliberately no MemoryHigh/MemoryMax: the goal is independent kill targets,
  # not per-pane budgets.
  #
  # The name must stay dash-free. systemd derives slice nesting from dashes, so
  # `zellij-panes.slice` would land under an implicit `zellij.slice` and the
  # panes would stop being direct children of the marked cgroup.
  systemd.user.slices.zjpanes = {
    Unit.Description = "Zellij panes, one cgroup each";
    Slice = {
      MemoryAccounting = true;
      ManagedOOMMemoryPressure = "kill";
    };
  };

  programs.zsh.initContent = lib.mkMerge [
    # Runs before the rest of zshrc: both branches below replace the shell, so
    # there is no point paying for completions and prompt setup first.
    (lib.mkBefore ''
      # Move each pane into a cgroup of its own.
      #
      # This is done from inside the pane rather than by handing zellij a
      # wrapper as $SHELL, because of how zellij decides what to write into the
      # resurrection layout. On every serialisation it reads the pane's live
      # foreground command and compares it against $SHELL (an exact string
      # match, args required empty). Anything that differs is recorded as an
      # explicit command, and after the next restart the server spawns it
      # directly -- outside the slice, silently unisolated. A wrapper can never
      # match, since the live process is always the shell it exec'd into.
      #
      # Re-execing into "$SHELL" itself keeps the comparison true, so the pane
      # stays absent from the layout and the isolation survives restarts.
      if [[ -n "$ZELLIJ" && -z "$ZJ_SCOPED" && -n "$SHELL" \
            && -S "''${XDG_RUNTIME_DIR:-/run/user/$UID}/bus" ]]; then
        exec ${pkgs.systemd}/bin/systemd-run \
          --user --scope --quiet --collect \
          --slice=zjpanes.slice \
          --description="zellij pane" \
          --setenv=ZJ_SCOPED=1 \
          -- "$SHELL"
      fi

      # Auto-attach to Zellij in graphical terminals.
      #   ~/.no-zellij     skip entirely and get a plain shell
      #   $ZELLIJ_SESSION  attach to a session other than "main"
      if [[ -z "$ZELLIJ" && -z "$SSH_CONNECTION" && ! -e "$HOME/.no-zellij" \
            && ( -n "$DISPLAY" || -n "$WAYLAND_DISPLAY" ) ]]; then
        exec ${pkgs.zellij}/bin/zellij attach --create "''${ZELLIJ_SESSION:-main}"
      fi
    '')
  ];
}
