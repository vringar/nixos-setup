{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-tc3bHU6cgod1K6uqWEjDQc8IEgCrPRR1WAHP+ofelw0=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [
    pkgs.git
    pkgs.jq
    pkgs.which
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';

  # The tracking tests write to one tracker DB and then assert their own
  # record is still inside a small get_recent(N) window. preCheck points the
  # whole check phase at a single HOME, so parallel test threads share that
  # DB and evict each other's records: which test loses the race varies by
  # version. Serializing removes the race for all of them rather than
  # skipping them one at a time, and costs little -- the suite is ~1s.
  checkFlags = [
    "--test-threads=1"
    # Waits on a PATH shim being exec'd by a spawned child; never observes
    # the shim run inside the Nix build sandbox (works outside it).
    "--skip=signalled_run_still_prints_captured_output"
    # Hardcode /usr/bin/printf and /bin/echo, which the Nix build sandbox
    # lacks (it only provides /bin/sh).
    "--skip=a_passthrough_keeps_its_own_shell_flag"
    "--skip=positional_arguments_are_not_interpreted_by_a_shell"
    "--skip=summary_arguments_are_not_interpreted_by_a_shell"
  ];
}
