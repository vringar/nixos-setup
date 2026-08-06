#!/usr/bin/env bash
# Create an Open WebUI API key without going through the UI, whose menu layout
# moves between releases. Signs in with your normal credentials, then asks the
# server for a key. Prints only the key; the password is never echoed or logged.
#
# If key creation is refused, API keys are switched off for this instance:
# Admin Panel -> Settings, look for "API Keys" (a global toggle), and check the
# group permission of the same name under user permissions.
set -euo pipefail

URL="${OPEN_WEBUI_URL:-http://sz1.fritz.box:8080}"

read -rp "Open WebUI email: " EMAIL
read -rsp "Open WebUI password: " PASSWORD
echo

TOKEN=$(
  jq -nc --arg e "$EMAIL" --arg p "$PASSWORD" '{email:$e,password:$p}' |
    curl -fsS -X POST "$URL/api/v1/auths/signin" \
      -H 'Content-Type: application/json' --data-binary @- |
    jq -r '.token'
)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "sign-in failed" >&2
  exit 1
fi

KEY=$(
  curl -fsS -X POST "$URL/api/v1/auths/api_key" \
    -H "Authorization: Bearer $TOKEN" | jq -r '.api_key'
)

if [ -z "$KEY" ] || [ "$KEY" = "null" ]; then
  echo "the server did not return a key — API keys are probably disabled" >&2
  exit 1
fi

echo
echo "API key:"
echo "$KEY"
echo
echo "Store it with:"
echo "  cd ~/nixos-setup/secrets && agenix -e open-webui-token.age"
echo "and put this single line in the file:"
echo "  OPEN_WEBUI_TOKEN=$KEY"
