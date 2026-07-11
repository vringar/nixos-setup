{pkgs, ...}: {
  home-manager.users.vringar = {imports = [../home-manager/graphical.nix ../home-manager/workstation.nix ../home-manager/ai.nix];};
  users.users.vringar.packages = import ../user/desktop.nix {inherit pkgs;};

  # Enable the X11 windowing system.
  # You can disable this if you're only using the Wayland session.
  services.xserver.enable = false;

  # Enable the KDE Plasma Desktop Environment.
  services.displayManager.sddm = {
    enable = true;
    wayland.enable = true;
  };
  services.desktopManager.plasma6.enable = true;
  programs.kdeconnect.enable = true;
  fonts.packages = with pkgs; [
    noto-fonts
    noto-fonts-color-emoji
    liberation_ttf
    fira-code
    fira-code-symbols
    mplus-outline-fonts.githubRelease
    dina-font
    nerd-fonts.jetbrains-mono
  ];

  # Configure keymap in X11
  services.xserver.xkb = {
    layout = "gb";
    variant = "extd";
  };

  # Configure console keymap
  console.keyMap = "uk";

  # Enable CUPS to print documents.
  services.printing.enable = true;

  # mDNS/DNS-SD so network printers (and other services) are auto-discovered.
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Enable sound with pipewire.
  services.pulseaudio.enable = false;
  security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    alsa.support32Bit = true;
    pulse.enable = true;
  };
  programs.firefox.enable = true;
  programs.partition-manager.enable = true;

  # Rootless podman for claude-sandbox: the sandbox starts a per-user
  # `podman system service` on the host and exposes its socket inside the
  # bubblewrap container as an OCI runner. Enabling this here (system level)
  # provides the newuidmap/newgidmap setuid wrappers and subuid/subgid ranges
  # rootless podman needs — home-manager cannot. Applies to sz1/sz3, which are
  # the NixOS hosts that get claude-sandbox via ai.nix.
  virtualisation.podman.enable = true;
  environment.systemPackages = with pkgs; [
    wl-clipboard
    nextcloud-client
  ];
}
