#!/usr/bin/env python3
"""PreToolUse/Bash hook: refuse `gh issue create` until the repo's issue template
has been looked at.

Projects put required fields in .github/ISSUE_TEMPLATE, and filing past the
template reads as low effort before a maintainer has read a word of the content
- paperless-ngx#13844 was closed partly for exactly that. `gh issue create
--title --body-file` bypasses the template silently, and nothing in the command
hints that one exists, so the mistake is invisible at the point of making it.

Rather than nag, this looks the templates up and names them. The lookup only
runs once the command is already an issue-create, so it costs nothing on
ordinary Bash calls, and any failure allows the command through: a hook that
blocks real work because GitHub is unreachable is worse than the mistake it
prevents.

Set ISSUE_TEMPLATE_OK=1 in the command to proceed once the template has
actually been read and filled in.
"""

import json
import re
import subprocess
import sys

MARKER = "ISSUE_TEMPLATE_OK"
API_TIMEOUT_SECONDS = 10

# `gh` as a command: line start, after whitespace, or after an env-var prefix.
GH_ISSUE_CREATE = re.compile(
    r"(^|[^\w-])gh\s.*\bissue\b.*\bcreate\b",
    re.DOTALL,
)
REPO_FLAG = re.compile(r"(?:-R|--repo)[\s=]+['\"]?([\w.-]+/[\w.-]+)")


def wants_issue_create(command):
    return bool(GH_ISSUE_CREATE.search(command))


def has_marker(command):
    return bool(re.search(rf"\b{MARKER}\s*=\s*1\b", command))


def target_repo(command):
    """The OWNER/REPO the issue would be filed against, if stated explicitly."""
    match = REPO_FLAG.search(command)
    return match.group(1) if match else None


def list_templates(repo):
    """Template filenames in the repo, [] if there are none, None if unknown.

    None means the question could not be answered - no network, no auth, a repo
    that does not exist - and the caller allows the command through on it.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/contents/.github/ISSUE_TEMPLATE",
                "--jq",
                ".[].name",
            ],
            capture_output=True,
            text=True,
            timeout=API_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        # A 404 is a real answer - the repo has no template directory.
        if "404" in result.stderr or "Not Found" in result.stderr:
            return []
        return None

    return [line for line in result.stdout.splitlines() if line.strip()]


def decide(command, lookup=list_templates):
    """Return a denial reason, or None to allow the command."""
    if not wants_issue_create(command) or has_marker(command):
        return None

    repo = target_repo(command)
    if repo is None:
        return (
            "Pass the target repo explicitly as `-R OWNER/REPO`. Without it `gh` "
            "guesses from the working directory, which is wrong inside a jj "
            "workspace, and this hook cannot check the repo's issue template."
        )

    templates = lookup(repo)
    if not templates:
        # No templates, or the lookup failed. Neither is worth blocking on.
        return None

    listed = ", ".join(sorted(templates))
    return (
        f"{repo} has issue templates ({listed}) and filing past them reads as low "
        "effort. Fetch the relevant one:\n\n"
        f"    gh api repos/{repo}/contents/.github/ISSUE_TEMPLATE/<name> "
        "--jq .content | tr -d '\\n' | base64 -d\n\n"
        "Fill in every required field. Do not invent content for fields you "
        "cannot fill - say what is missing and why. Then re-run the same command "
        f"prefixed with {MARKER}=1."
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    reason = decide(command)
    if reason is None:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
