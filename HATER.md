# The Hater's Code Review

**Change**: `otvtuqsu` (fb7a171d) — personal bpmnlint plugin + camunda-modeler plugin wiring (4 files in scope)
**Date**: 2026-04-24
**Verdict**: Begrudgingly Adequate

Scope reviewed: `apps/bpmnlint/default.nix`, `apps/camunda-modeler/default.nix`, `home.nix` (camunda-modeler routing), `npins/sources.json` (plugin pin). Everything else in `@` (c8ctl, jj scripts, nvim dotfiles, element-templates, sandbox tweaks) is out of scope per the request. I looked at the code, read the upstream `bpmnlint/bin/bpmnlint.js` and `lib/resolver/node-resolver.js`, the plugin repo at the pinned revision, and test-built the derivation to verify the patched `recommended.js` actually loads and resolves the plugin from an arbitrary CWD. It does.

For a personal-dotfiles-grade hack designed to be "undetectable from outside this machine," this is — reluctantly — a reasonable shape. The two real smells are rsync pulled in for a job cp can do in two lines, and the rule list duplicated by hand when the upstream plugin already publishes it. Everything else is minor.

## Issues

### 1. Hand-duplicated rule list will rot silently
**Severity**: Major
**File**: `/home/stefan/projects/other/nixos-setup/apps/bpmnlint/default.nix`
**Lines**: 38-39

You hardcode `'camunda-ai/from-ai-tool-call': 'error'` and `'camunda-ai/tool-call-result': 'info'` in the `recommended.js` override. The plugin repo *already* ships `bpmnlint-plugin-camunda-ai/.bpmnlintrc` with exactly this block. If the plugin adds a third rule, renames one, or changes a default severity, this derivation will silently diverge: new rules invisible in `bpmnlint:recommended`, renamed rules either silently dropped or exploding with `cannot resolve rule <old-name>` only when someone actually runs the linter. You also asked explicitly about this in angle (5) — yes, the coupling is real, and yes, the failure mode is silent for "new/renamed" and loud-at-runtime for "removed."

The plugin's `.bpmnlintrc` is right there. Read it at eval time and splice the rules in:

```nix
pluginConfig = builtins.fromJSON (builtins.readFile
  "${sources.bpmnlint-aitools}/bpmnlint-plugin-camunda-ai/.bpmnlintrc");
pluginRulesJs = builtins.toJSON pluginConfig.rules;
```

Then in the heredoc: `rules: { ...upstream.rules, ...${pluginRulesJs} }`. One source of truth, no drift, three extra lines of Nix. There is no reason not to do this.

### 2. rsync as a build dep for what `cp` handles in one line
**Severity**: Minor
**File**: `/home/stefan/projects/other/nixos-setup/apps/camunda-modeler/default.nix`
**Lines**: 19-25

The Modeler plugin only needs two things: `index.js` (entry point, 78 bytes) and `dist/` (the webpacked client). The repo root also contains `client/`, `webpack.config.js`, a top-level `package.json`, `.bpmnlintrc`, `package-lock.json`, and the nested `bpmnlint-plugin-camunda-ai/` which you explicitly exclude. You're pulling in `pkgs.rsync` — a whole external tool — to blacklist four paths when a whitelist of two paths would copy the right thing with coreutils:

```bash
install -d $out/share/camunda-modeler/resources/plugins/camunda-ai-lint
cp -a \
  ${sources.bpmnlint-aitools}/index.js \
  ${sources.bpmnlint-aitools}/dist \
  $out/share/camunda-modeler/resources/plugins/camunda-ai-lint/
```

That's it. No rsync, no exclude list to maintain, no ambient "what if a new top-level file appears in the repo and gets dragged along" question. You even identified `index.js` and `dist/client.js` as the required bits in your own comment — lead with that, don't work around it by excluding everything else.

The rsync version also drags `webpack.config.js`, `package-lock.json`, and the top-level `package.json` into the install prefix for no reason. Modeler ignores them, but shipping dev build artifacts into `resources/plugins/` is sloppy.

### 3. Commit message is absent
**Severity**: Minor
**File**: jj change `fb7a171d`

Empty description on `@`. I know it's not done yet — the whole change is currently a mixed bag (c8ctl upgrade, nvim dotfiles, jj push-hook scripts, bpmnlint wiring) and presumably gets split before it lands on main. Fine. Just don't push this as a single commit with no description. When you split, give the bpmnlint bits their own commit with a message that says *why* the `bpmnlint:recommended` hijack exists and who is expected to notice (nobody). Future-you will not remember.

### 4. `recommended-upstream.js` leaks as a resolvable config name
**Severity**: Nitpick
**File**: `/home/stefan/projects/other/nixos-setup/apps/bpmnlint/default.nix`
**Lines**: 52-54

`postPatch` copies `recommended.js` to `recommended-upstream.js` in the same `config/` dir. That means anyone who knows the trick can write `{"extends": "bpmnlint:recommended-upstream"}` and dodge the silent plugin injection. Given your threat model ("undetectable from *outside* this machine") this is probably fine — no extending artifact in a shared repo names `recommended-upstream`, so the stealth holds. But it's a latent escape hatch sitting in your config dir. If it bothers you, stash it at a path bpmnlint's scheme resolver can't reach, e.g. `$out/lib/node_modules/bpmnlint/internal/recommended-upstream.js`, and `require('../internal/recommended-upstream')` from the replacement file.

I wouldn't lose sleep over it. Flagging because you asked for hidden coupling.

## Things That Are Actually Fine

I am required by my own credibility to admit these before I stop writing:

- **NODE_PATH wrapping (bpmnlint/default.nix:47-48)**: Correct. Upstream's `NodeResolver` uses `createScopedRequire(cwd())`, so the bundled plugin is unreachable from user projects without an extra resolution hint. NODE_PATH is exactly the mechanism node consults as a fallback, and I verified end-to-end that `bpmnlint valid.bpmn` from `/tmp/foo` resolves `camunda-ai/*` rules out of the store. This is not masking a design smell; it's the least-invasive fix that doesn't require patching bpmnlint's CLI source. Leave it.
- **`postPatch` stash of the original recommended**: Yes, it's a little cute — but the alternative (inlining upstream's 25-rule block into the Nix expression) is strictly worse because it silently drifts when npins bumps bpmnlint. The spread-from-upstream approach is the right call.
- **The Nix heredoc**: Nix string interpolation correctly strips common leading whitespace, so the generated file is flush-left JS. I checked. Builds, loads, runs.
- **`home.nix` routing via `camunda-modeler = import ./apps/camunda-modeler {...}`**: The `writeShellScriptBin` wrapper around `lib.getExe camunda-modeler` correctly picks up the override instead of `pkgs.camunda-modeler`. No sloppiness here.
- **Two-derivation split (bpmnlint bundles the lint plugin; camunda-modeler bundles the Modeler wrapper)**: This is the right decomposition. The lint plugin and the Modeler plugin have different install prefixes and different runtime consumers; trying to share install logic would be worse than the current small duplication of `sources.bpmnlint-aitools` references.

## Closing Remarks

Personal machine hack, narrow scope, works. I had to work to find things to complain about. The rsync dependency is genuinely pointless and should go. The rule-list duplication is a small time bomb worth defusing now — the fix is three lines of Nix. Everything else is either nitpick territory or actually correct despite looking clever. Write the commit message before you push.

Now stop making me read BPMN XML.
