# Records what memory looked like, so the next incident can be read rather than
# reconstructed by inference.
#
# The machine keeps no historical memory metrics -- no sysstat, no exporter, no
# collector -- and the two most useful sources have no history of their own: the
# ARC is a live kstat, and the reclaim and paging figures are counters that only
# mean something as a difference between two readings. After an out-of-memory
# event none of it is recoverable, which is how a past kill ends up being argued
# from numbers taken hours later.
{pkgs, ...}: let
  mem-sampler = pkgs.callPackage ../apps/mem-sampler {};

  cgroups = "/sys/fs/cgroup";

  # Direct children of this slice are the units a pressure-based kill chooses
  # between, so sampling them is what names a victim afterwards.
  paneSlice = "${cgroups}/user.slice/user-1000.slice/user@1000.service/zjpanes.slice";

  # Watched alongside the panes so that a charge can be attributed rather than
  # merely observed: builds driven through the daemon are accounted here, and
  # without it there is no way to tell those apart from a compiler running
  # directly in a shell -- which is the difference between capping the daemon's
  # parallelism and capping the build tool's.
  systemSlice = "${cgroups}/system.slice";
in {
  environment.systemPackages = [mem-sampler];

  systemd.services.mem-sampler = {
    description = "Sample memory gauges and counters into the journal";
    wantedBy = ["multi-user.target"];

    serviceConfig = {
      # One resident process rather than a timer: at this interval the unit
      # start and stop lines systemd logs per activation would outnumber the
      # samples themselves.
      ExecStart = builtins.concatStringsSep " " [
        "${mem-sampler}/bin/mem-sampler"
        "--interval 10"
        "--cgroup ${paneSlice}"
        "--cgroup ${systemSlice}"
      ];
      Restart = "always";
      RestartSec = 5;

      # Ten seconds beats the thirty a pressure rule must sustain before acting,
      # so a kill decision has several samples inside its own decision window.
      #
      # An observer that is killed alongside what it observes records nothing
      # about the interesting part, so it is exempted from both killers and kept
      # small enough that the exemption costs nothing.
      OOMScoreAdjust = -900;
      ManagedOOMMemoryPressure = "auto";
      MemoryMax = "64M";

      DynamicUser = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      NoNewPrivileges = true;
      RestrictAddressFamilies = ["AF_UNIX"];
      SystemCallFilter = ["@system-service"];
      CapabilityBoundingSet = [];
    };
  };
}
