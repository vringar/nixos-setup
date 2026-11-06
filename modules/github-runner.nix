# Self-hosted GitHub Actions runner for vringar/zabka_it.
#
# Personal accounts cannot own account-level runners, so each repo gets its
# own runner. Only private repos belong here: a fork PR against a public repo
# would run arbitrary code on this machine.
#
# The PAT (fine-grained, scoped to the repo, Administration: read/write) is
# only used to fetch runner registration tokens at service start. Jobs get
# their own GITHUB_TOKEN with whatever the workflow's `permissions:` grants.
# sz1 is shut down regularly; GitHub queues jobs until the runner is back.
{
  config,
  pkgs,
  ...
}: {
  age.secrets.github-runner-zabka-it.file = ../secrets/github-runner-zabka-it.age;

  services.github-runners.zabka-it = {
    enable = true;
    url = "https://github.com/vringar/zabka_it";
    tokenFile = config.age.secrets.github-runner-zabka-it.path;
    # Re-registering after a reboot or a rename replaces the stale entry
    # instead of failing on the name clash.
    replace = true;
    extraLabels = ["nixos"];
    extraPackages = [
      pkgs.git
      config.nix.package
    ];
  };
}
