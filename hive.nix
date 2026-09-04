# colmena config
let
  sources = import ./npins;

  # systemd has no readiness probe: a unit counts as started the moment its
  # process forks, so a server that accepts connections and then answers
  # nothing looks perfectly healthy — which is how a nix-serve whose workers
  # all aborted stayed "active (running)" while t20 timed out against it.
  # ExecStartPost failing marks the unit failed, so `colmena apply` reports a
  # service that came up but does not serve, instead of leaving it for the
  # first user to discover. Liveness only: it says the endpoint answers, not
  # that the service is correct.
  httpReady = {
    pkgs,
    url,
    retries ? 10,
  }:
    "${pkgs.curl}/bin/curl --fail --silent --show-error --output /dev/null"
    + " --connect-timeout 2 --max-time 5"
    + " --retry ${toString retries} --retry-delay 2 --retry-all-errors ${url}";
in {
  meta = {
    nixpkgs = import sources.nixpkgs {config.allowUnfree = true;};
  };

  defaults = {...}: {
    # Available to every host module, not just the per-host blocks below.
    _module.args.httpReady = httpReady;

    imports = [
      (import "${sources.home-manager}/nixos")
      (import "${sources.agenix}/modules/age.nix")
      ./modules/baseline.nix
    ];

    home-manager.sharedModules = [
      (import "${sources.plasma-manager}/modules")
    ];

    deployment.replaceUnknownProfiles = true;
    nixpkgs.flake.source = sources.nixpkgs;

    # Pin system-wide nixpkgs to npins
    nix.nixPath = ["nixpkgs=${sources.nixpkgs}"];
    nix.channel.enable = false;
  };

  sz1 = {
    pkgs,
    lib,
    config,
    ...
  }: let
  in {
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      ./hardware/sz1.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./modules/wg-sect.nix
      ./modules/local-llm.nix
      ./modules/mem-sampler.nix
      ./modules/paperless.nix
      {home-manager.users.vringar = import ./home-manager/ghidra.nix;}
      {home-manager.users.vringar = import ./home-manager/zellij-resilient.nix;}
    ];
    nix.settings.secret-key-files = ["/etc/nix/signing-key.sec"];

    # lix-src is pinned to Lix's main branch, so this host runs a Lix ahead of
    # what nixpkgs validates: nixpkgs only tests nix-serve-ng against
    # lixPackageSets.stable. Lix main dropped `enum class HashFormat` and
    # `Hash::to_string()` from libutil/hash.hh, but nix-serve-ng still calls
    # them, so it stops compiling. `to_base32()` renders the same
    # "<algo>:<base32>" string the old to_string(Base32, includeType=true) did,
    # and exists in both the old and new headers, so this patch is independent
    # of where lix-src happens to point. --replace-fail means a nix-serve-ng
    # bump that fixes this upstream breaks the build here rather than going
    # unnoticed.
    nixpkgs.overlays = [
      (final: prev: {
        nix-serve-ng =
          final.haskell.lib.compose.overrideCabal (old: {
            postPatch =
              (old.postPatch or "")
              + ''
                substituteInPlace cbits/nix.cpp \
                  --replace-fail 'narHash.to_string(nix::HashFormat::Base32, true)' \
                                 'narHash.to_base32()'
              '';
          })
          prev.nix-serve-ng;
      })
    ];

    # Serve sz1's store as a binary cache so t20 can *substitute* its aarch64
    # closure instead of depending on colmena pushing every path. Reuses the
    # signing key sz1 already has and the public key t20 already trusts (see
    # scripts/sign-for-t20.sh), so no new key material is involved.
    services.nix-serve = {
      enable = true;
      # The default nix-serve is a Perl app needing Nix's Perl bindings, which
      # Lix does not ship: workers die on `Can't locate Nix/Config.pm`, so the
      # listening socket accepts connections that are never served and clients
      # hang until they time out. nix-serve-ng is a drop-in with no Perl.
      package = pkgs.nix-serve-ng;
      secretKeyFile = "/etc/nix/signing-key.sec";
      # openFirewall would open the port on every interface, including the
      # wg-sect tunnel; scoped to the LAN NIC with Open WebUI's rule instead.
      openFirewall = false;
    };

    # Answering /nix-cache-info is the whole job of this service, and the one
    # thing a broken Perl build never managed.
    systemd.services.nix-serve.serviceConfig.ExecStartPost = httpReady {
      inherit pkgs;
      url = "http://127.0.0.1:5000/nix-cache-info";
    };

    # LAN NIC only, never the wg-sect tunnel. enp4s0 is a PCI-path name,
    # stable unless the NIC is replaced or moved — if it ever changes, this
    # rule stops matching and the cache goes unreachable from the LAN rather
    # than becoming over-exposed. modules/local-llm.nix opens Open WebUI's
    # port against the same interface; the lists merge.
    networking.firewall.interfaces."enp4s0".allowedTCPPorts = [
      5000 # nix-serve binary cache
    ];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";
    services.teamviewer.enable = true;
    system.stateVersion = "25.05";
  };

  t20 = {...}: {
    imports = [
      ./hardware/pi.nix
      ./t20/caddy.nix
    ];

    # Stable-privacy SLAAC derives the interface ID from the prefix, so a
    # prefix change (reconnect, move) would break the FritzBox IPv6 exposure
    # rule and the AAAA record. Pin the ID instead: t20 is always <prefix>::443.
    # Same UUID as the auto-generated profile so this replaces it.
    networking.networkmanager.ensureProfiles.profiles.wired = {
      connection = {
        id = "Wired connection 1";
        uuid = "fe873ff9-4ca7-309e-bbf9-5dc1fe85e60f";
        type = "ethernet";
        interface-name = "enu1u1u1";
      };
      ipv4.method = "auto";
      ipv6 = {
        method = "auto";
        addr-gen-mode = "eui64";
        token = "::443";
      };
    };

    boot.kernel.sysctl = {
      "net.ipv4.ip_forward" = 1;
      "net.ipv6.conf.all.forwarding" = 1;
    };

    security.sudo.wheelNeedsPassword = false;

    # The sz1 key authorizes signatures on paths colmena pushes after
    # scripts/sign-for-t20.sh signs them — it is load-bearing for deploys, not
    # only for the substituter below.
    nix.settings.trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "sz1.fritz.box:CB1Zd3dpBNECfzeGVpkDNJYds4O/eKJhV2Tlx2NGqEc="
    ];

    # Pull from sz1 first: it has already built this host's closure, and the Pi
    # cannot realistically build anything itself. `extra-` so cache.nixos.org
    # stays in the list. An unreachable substituter is a warning, not an error,
    # so t20 still deploys when sz1 is off or in Windows.
    nix.settings.extra-substituters = ["http://sz1.fritz.box:5000"];

    deployment.tags = ["personal"];
    deployment.targetHost = "t20.fritz.box";
    deployment.targetUser = "vringar";

    system.stateVersion = "25.05";
  };

  sz3 = {pkgs, ...}: {
    imports = [
      (import "${sources.lix-module}/module.nix" {
        lix = sources.lix-src;
        versionSuffix = sources.lix-src.revision;
      })
      ./hardware/sz3.nix
      ./modules/bluetooth.nix
      ./modules/desktop.nix
      ./modules/wg-sect.nix
    ];

    deployment.tags = ["personal"];
    deployment.allowLocalDeployment = true;
    deployment.targetUser = "vringar";

    # This machine lives outside the house, so authenticate SSH against tailnet
    # ACLs instead of an on-disk authorized_keys file. Note this is
    # extraSetFlags, not extraUpFlags: the latter is only applied when an
    # authKeyFile is set, which would make it a no-op on an already
    # authenticated node.
    services.tailscale.extraSetFlags = ["--ssh"];

    users.users.vringar.extraGroups = ["docker"];
    users.users.sash = import ./user/sash.nix {inherit pkgs;};
    virtualisation.docker = {
      enable = true;
      storageDriver = "btrfs";
    };
    programs.steam.enable = true;
    system.stateVersion = "24.11";
  };
}
