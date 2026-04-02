# Local override of nixpkgs claude-code until nixpkgs updates past 2.1.88
# (2.1.88 was yanked from npm; 2.1.90 is the current release)
# package-lock.json is the same as nixpkgs 2.1.88 — same sharp dep versions.
{pkgs, ...}:
pkgs.buildNpmPackage (finalAttrs: {
  pname = "claude-code";
  version = "2.1.90";

  src = pkgs.fetchzip {
    url = "https://registry.npmjs.org/@anthropic-ai/claude-code/-/claude-code-${finalAttrs.version}.tgz";
    hash = "sha256-4/hqWrY2fncQ8p0TxwBAI+mNH98ZDhjvFqB9us7GJK0=";
  };

  npmDepsHash = "sha256-izy3dQProZIdUF5Z11fvGQOm/TBcWGhDK8GvNs8gG5E=";

  strictDeps = true;

  postPatch = ''
    cp ${./package-lock.json} package-lock.json

    # https://github.com/anthropics/claude-code/issues/15195
    substituteInPlace cli.js \
          --replace-fail '#!/bin/sh' '#!/usr/bin/env sh'
  '';

  dontNpmBuild = true;

  env.AUTHORIZED = "1";

  postInstall = ''
    wrapProgram $out/bin/claude \
      --set DISABLE_AUTOUPDATER 1 \
      --set-default FORCE_AUTOUPDATE_PLUGINS 1 \
      --set DISABLE_INSTALLATION_CHECKS 1 \
      --unset DEV \
      --prefix PATH : ${
      pkgs.lib.makeBinPath (
        [pkgs.procps]
        ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
          pkgs.bubblewrap
          pkgs.socat
        ]
      )
    }
  '';

  meta = with pkgs.lib; {
    description = "Agentic coding tool that lives in your terminal";
    homepage = "https://github.com/anthropics/claude-code";
    license = licenses.unfree;
    mainProgram = "claude";
  };
})
