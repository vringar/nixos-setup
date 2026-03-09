{...}: {
  programs.zsh.initContent = ''
    # Auto-attach to Zellij "main" session in graphical terminals
    if [[ -z "$ZELLIJ" && -z "$SSH_CONNECTION" && ( -n "$DISPLAY" || -n "$WAYLAND_DISPLAY" ) ]]; then
      exec zellij attach --create main
    fi
  '';
}
