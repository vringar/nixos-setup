let
  # readFile keeps the file's trailing newline. agenix joins recipients with
  # newlines, so an unstripped key produces a blank line — an empty recipient —
  # as soon as a list holds more than one key.
  vringar = builtins.replaceStrings ["\n"] [""] (builtins.readFile ../home-manager/files/ssh/github_key.pub);
  # t20's SSH host key. Secrets for services that start at boot must be
  # decryptable without /home, so they are encrypted to the host rather than
  # to a user key. Obtain with: ssh-keyscan -t ed25519 t20.fritz.box
  t20 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPuTgq6u56J/wbsyrFWBhx2unFjG71s1tbTCiB7J63F8";
  sz1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPsRDN/nclBLT38c9X+5Ie0FTQ7nU5vGTNnZ1c9CDKIK";
in {
  "wg-sect.age".publicKeys = [vringar];
  # INWX DNS API credentials for Caddy's DNS-01 wildcard issuance.
  # Contents (environment file): INWX_USER=... / INWX_PASSWORD=...
  "inwx.age".publicKeys = [vringar t20];
  # Open WebUI API key for the corpus reconciler, which runs at boot before
  # /home is available — hence the host key rather than a user key. Create the
  # key in the UI under Settings → Account → API keys.
  # Contents (environment file): OPEN_WEBUI_TOKEN=sk-...
  "open-webui-token.age".publicKeys = [vringar sz1];
}
