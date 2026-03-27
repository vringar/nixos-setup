{pkgs, ...}: {
  environment.systemPackages = [pkgs.wireguard-tools];

  systemd.services."wg-quick-sect" = {
    description = "WireGuard tunnel sect";
    after = ["network.target" "home.mount"];
    wants = ["home.mount"];
    wantedBy = []; # manual start: systemctl start wg-quick-sect
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStartPre = "${pkgs.age}/bin/age -d -i /home/vringar/.ssh/github_key -o /run/wg-sect.conf ${../secrets/wg-sect.age}";
      ExecStart = "${pkgs.wireguard-tools}/bin/wg-quick up /run/wg-sect.conf";
      ExecStop = "${pkgs.wireguard-tools}/bin/wg-quick down /run/wg-sect.conf";
      ExecStopPost = "${pkgs.coreutils}/bin/rm -f /run/wg-sect.conf";
    };
  };
}
