# Open WebUI plugins, pinned by hash.
#
# Plugins are Python that runs inside Open WebUI's process with access to every
# chat, so installing them from the marketplace UI means whatever the author
# published most recently, applied silently. Pinning here means upgrades are a
# tag and hash bump, reviewable as a diff, at a moment of our choosing.
#
# `valves` is the plugin's own configuration, applied by the sync service.
{fetchurl}: {
  usage_display = {
    name = "Token Usage & Cost Display";
    description = "Token counts and context-window utilization under each reply";
    # https://github.com/SmetDenis/openwebui-token-usage-display
    src = fetchurl {
      url = "https://raw.githubusercontent.com/SmetDenis/openwebui-token-usage-display/v2.5.2/usage_display.py";
      hash = "sha256-O1gz/L57MSiPEdZgggWmzkMnryHl59sfMF3AV2sxp4A=";
    };
    # A filter only applies to every model when it is enabled globally;
    # otherwise it has to be attached to each model preset by hand.
    global = true;
    valves = {
      # The point of installing this: how full the context is, which is the
      # signal for "the model is about to start losing earlier details".
      show_context_window = true;
      # Detection would otherwise guess. models.dev has never heard of a
      # community fine-tune, and probing llama-swap reports whichever model
      # happens to be loaded. The size is set by the caller from the same
      # value the server is started with, so the two cannot drift.
      # context_size_override is filled in by the caller.

      # Everything here is a local model on hardware that is already paid for,
      # so per-token pricing is noise — and `estimate` mode would invent
      # numbers from an unrelated model's price list.
      show_cumulative_cost = false;
      # No outbound lookups: this service handles private writing, and neither
      # context sizes nor prices need a third party's opinion.
      fetch_context_from_modelsdev = false;
      fetch_prices_from_modelsdev = false;
      debug_mode = false;
    };
  };
}
