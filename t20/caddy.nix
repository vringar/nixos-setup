# Public HTTPS edge for the family LLM service (docs/local-llm-service.md,
# phase 2.5). Only t20:443 faces the internet; it terminates TLS and proxies to
# Open WebUI on sz1, which stays LAN-bound. The certificate is a DNS-01
# *wildcard* so `chat` never appears in Certificate Transparency logs (D9) —
# that needs the INWX solver, since stock Caddy speaks only HTTP-01 and HTTP-01
# cannot issue a wildcard at all.
{
  pkgs,
  lib,
  config,
  httpReady,
  ...
}: let
  # Issuance is shared by both site blocks below, so it lives in one place.
  #
  # `resolvers` is load-bearing: the *.home.zabka.it CNAME also matches
  # _acme-challenge.home.zabka.it, so any recursive resolver that looked the
  # challenge name up while no TXT existed caches that CNAME for its full hour
  # and answers from cache instead of ever seeing the record. Caddy then waits
  # for a propagation it cannot observe and never asks Let's Encrypt to
  # validate. Asking INWX's own nameservers side-steps every cache in the path
  # — t20 resolves through Tailscale's MagicDNS, so there is more than one.
  inwxTls = ''
    tls {
      dns inwx {
        username {env.INWX_USER}
        password {env.INWX_PASSWORD}
      }
      resolvers ns.inwx.de ns2.inwx.de ns3.inwx.eu
    }
  '';
in {
  services.caddy = {
    enable = true;
    package = pkgs.caddy.withPlugins {
      plugins = ["github.com/caddy-dns/inwx@v0.4.1"];
      hash = "sha256-jhZRekdz/aWA44mNIxwfLPbM/BYjYcLMFEsnbVVKxlQ=";
    };

    # ONE site block for the whole wildcard, services dispatched by host
    # matcher inside it. A named vhost (chat.home.zabka.it) would make Caddy
    # issue a *named* cert for it — straight into the CT logs, defeating the
    # point of the wildcard. Adding a service later = new handle block, no ACME
    # round trip.
    virtualHosts."*.home.zabka.it".extraConfig = ''
      ${inwxTls}

      @chat host chat.home.zabka.it
      handle @chat {
        # sz1 stays on plain HTTP behind the LAN firewall; SSE streaming from
        # llama-server passes through reverse_proxy without extra buffering
        # configuration.
        reverse_proxy http://sz1.fritz.box:8080
      }

      # Unknown *.home.zabka.it names: don't reveal what exists.
      handle {
        respond "" 404
      }

      # sz1 unreachable (D14): serve the diagnosis page rendered by
      # edge-status.timer instead of a bare 502.
      handle_errors {
        root * /run/edge-status
        rewrite * /status.html
        file_server
      }
    '';

    # A wildcard covers exactly one label, so the bare name needs its own
    # certificate — which is fine to have in the CT logs, since DynDNS already
    # publishes home.zabka.it in public DNS. Reaching this page proves the
    # whole path works (DNS, port forward, TLS, Caddy) without depending on
    # sz1 being awake.
    virtualHosts."home.zabka.it".extraConfig = ''
      ${inwxTls}

      header Content-Type "text/html; charset=utf-8"
      respond "<!doctype html><meta name=viewport content=width=device-width><title>t20</title><h1>I'm alive</h1>" 200
    '';
  };

  # INWX API credentials for the DNS-01 challenge. Caddy starts at boot, before
  # /home is available, so the secret is encrypted to t20's SSH host key rather
  # than to a user key.
  age.secrets.inwx.file = ../secrets/inwx.age;
  systemd.services.caddy.serviceConfig = {
    EnvironmentFile = config.age.secrets.inwx.path;

    # Caddy signals readiness itself (Type=notify), so this only has to catch
    # the cases where it comes up serving nothing. The admin API is local and
    # always present; certificate issuance happens afterwards and is not gated
    # here — a wildcard that fails DNS-01 still shows up in the journal.
    ExecStartPost = httpReady {
      inherit pkgs;
      url = "http://127.0.0.1:2019/config/";
    };
  };

  networking.firewall.allowedTCPPorts = [80 443];

  # A Caddyfile that nix evaluates happily can still be rejected by the config
  # adapter, which only runs when Caddy starts — a green build then deploys an
  # edge that never comes up. Adapting the generated config at build time moves
  # that failure left; it exercises plugin directives too, since the check runs
  # the same caddy build the host will.
  system.checks = [
    (pkgs.runCommand "caddy-config-adapts" {} ''
      ${lib.getExe config.services.caddy.package} adapt \
        --config ${config.services.caddy.configFile} \
        --adapter caddyfile >/dev/null
      touch $out
    '')
  ];

  # D14: when sz1 is unreachable, tell the family member *why*. A timer probes
  # sz1 each minute and renders the page handle_errors serves: port open →
  # transient; ping but no port → booted into Windows; no ping → powered off.
  systemd.services.edge-status = {
    description = "Render sz1 reachability status page for the Caddy edge";
    serviceConfig.Type = "oneshot";
    script = ''
      if ${pkgs.coreutils}/bin/timeout 2 ${pkgs.bash}/bin/bash \
        -c 'exec 3<>/dev/tcp/sz1.fritz.box/8080' 2>/dev/null; then
        msg="sz1 is reachable — this page should be gone in a moment. Retry."
      elif ${pkgs.iputils}/bin/ping -c1 -W2 sz1.fritz.box >/dev/null 2>&1; then
        msg="sz1 is currently booted into Windows. Chat is unavailable until it is back in Linux."
      else
        msg="sz1 is powered off. Ask Stefan to switch it on."
      fi
      printf '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>chat status</title></head><body style="font-family:sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem"><h1>Chat is taking a break</h1><p>%s</p></body></html>' "$msg" \
        > /run/edge-status/status.html.tmp
      mv /run/edge-status/status.html.tmp /run/edge-status/status.html
    '';
  };
  systemd.timers.edge-status = {
    wantedBy = ["timers.target"];
    timerConfig = {
      OnBootSec = "30s";
      OnUnitActiveSec = "60s";
    };
  };
  # tmpfs, so the once-a-minute rewrite never touches the SD card.
  systemd.tmpfiles.rules = ["d /run/edge-status 0755 root root -"];
}
