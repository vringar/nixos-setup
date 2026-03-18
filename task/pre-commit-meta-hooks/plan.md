# Task: Fix pre-commit meta hooks on Nix

## Problem

When running `pre-commit run --all-files` in a repo that uses `repo: meta` hooks
(`check-hooks-apply`, `check-useless-excludes`), the following error occurs:

```
/nix/store/.../python3.13: Error while finding module specification for
'pre_commit.meta_hooks.check_hooks_apply'
(ModuleNotFoundError: No module named 'pre_commit')
```

## Root Cause

`_entry()` in `pre_commit/clientlib.py` builds the shell command used to invoke
meta hooks:

```python
def _entry(modname: str) -> str:
    return f'{shlex.quote(sys.executable)} -m pre_commit.meta_hooks.{modname}'
```

This spawns a new process using the bare `sys.executable` path. On Nix, the
pre-commit binary is a wrapper script that calls the real Python interpreter with
`site.addsitedir()` injections for all its dependencies. When a subprocess is
spawned directly via `sys.executable`, those injections are not present, so
`import pre_commit` fails.

## Precedent

nixpkgs already contains an identical fix for the `pygrep` language in
`pkgs/by-name/pr/pre-commit/pygrep-pythonpath.patch`, which passes `PYTHONPATH`
via `sys.path` to the subprocess:

```diff
-    return xargs(cmd, file_args, color=color)
+    return xargs(cmd, file_args, color=color, env={ "PYTHONPATH": ':'.join(sys.path) })
```

## Fix

Apply the same approach to `clientlib.py`. The patch is in `meta-hooks-pythonpath.patch`.

### Changes required in nixpkgs

**1. Copy the patch file** into the package directory:

```
pkgs/by-name/pr/pre-commit/meta-hooks-pythonpath.patch
```

**2. Add it to the `patches` list** in `pkgs/by-name/pr/pre-commit/package.nix`:

```nix
patches = [
  ./languages-use-the-hardcoded-path-to-python-binaries.patch
  ./hook-tmpl.patch
  ./pygrep-pythonpath.patch
  ./meta-hooks-pythonpath.patch  # <-- add this line
];
```

## Verification

Build the patched package and confirm it compiles and all tests pass:

```bash
nix-build -A pre-commit
```

Expected: 715 passed, 1 skipped, 3 xfailed (same as unpatched).

The patch was tested locally by overriding pre-commit via `overrideAttrs` in a
home-manager config. All nixpkgs tests pass with the patch applied.

## Notes

- The fix is intentionally minimal — one new line, mirrors the pygrep precedent exactly.
- `env PYTHONPATH=...` is used (rather than setting env on `xargs`) because the
  meta hook entry is a plain string that goes through `shlex.split()` in
  `lang_base.hook_cmd()`, not through an env-aware code path.
- This is a Nix-specific issue; the fix is harmless on other platforms since
  `PYTHONPATH` will simply be set to the same paths Python already knows about.
