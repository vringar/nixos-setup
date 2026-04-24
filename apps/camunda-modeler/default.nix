# Overrides nixpkgs `camunda-modeler` to drop a personal Modeler plug-in
# (camunda-ai-lint) into resources/plugins/. The wrapper ships pre-built
# (dist/client.js committed), so Nix just has to copy the two runtime bits.
#
# Private to this machine — the plugin repo is referenced only here and in
# apps/bpmnlint. Nothing in shared .bpmnlintrc files changes.
{
  pkgs,
  sources,
}:
pkgs.camunda-modeler.overrideAttrs (old: {
  postInstall =
    (old.postInstall or "")
    + ''
      install -d $out/share/camunda-modeler/resources/plugins/camunda-ai-lint
      cp -a \
        ${sources.bpmnlint-aitools}/index.js \
        ${sources.bpmnlint-aitools}/dist \
        $out/share/camunda-modeler/resources/plugins/camunda-ai-lint/
    '';
})
