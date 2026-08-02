#!/usr/bin/env bash
# Phase 2 of docs/local-llm-service.md: create the Open WebUI state dataset,
# deploy the frontend, and verify it comes up and can see the llama-swap
# backend. Run OUTSIDE the sandbox, on sz1; prompts for sudo.
# Output captured to deploy-phase2.log.
#
# Account creation and disabling signup are deliberately NOT in here — the
# first account registered becomes admin, so that step is manual, and
# ENABLE_SIGNUP flips to "False" in a second deploy afterwards.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
{
  set -x
  # 1. Child of zpool/llm so the 200G quota still bounds it (ZFS quotas cover
  #    descendants). Mounted where systemd puts DynamicUser state, so systemd
  #    owns the permissions and we never chown a runtime-allocated UID.
  if ! sudo zfs list zpool/llm/open-webui >/dev/null 2>&1; then
    sudo zfs create -o mountpoint=/var/lib/private/open-webui zpool/llm/open-webui || exit 1
  fi

  # 2. Deploy: open-webui unit, firewall opening, the mount declaration.
  bash rebuild.sh || exit 1

  # 3. Unit is running, not just started-and-crashed.
  systemctl is-active open-webui || exit 1

  # 4. The UI answers locally.
  curl -sf -o /dev/null -w 'webui HTTP %{http_code}\n' http://127.0.0.1:8080/ || exit 1

  # 5. The backend it was pointed at is reachable and lists the candidates.
  curl -sf http://127.0.0.1:9292/v1/models || exit 1
  echo

  # 6. State really landed on the dataset rather than on zpool/var, and the
  #    quota that is supposed to bound it is still in place.
  findmnt -no SOURCE /var/lib/private/open-webui || exit 1
  stat -c '%a %n' /var/lib/private || exit 1
  zfs get -H -o name,property,value quota zpool/llm || exit 1
} 2>&1 | tee deploy-phase2.log
exit "${PIPESTATUS[0]}"
