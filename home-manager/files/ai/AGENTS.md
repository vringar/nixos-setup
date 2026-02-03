Print a 🌍 as part of your response to show you have read this file.

## Code Quality
- Prefer small, focused changes - Make minimal changes to accomplish the task; avoid unnecessary refactoring
- Don't delete code without asking - Comment out or confirm before removing functionality
- Preserve existing style - Match the formatting/conventions of surrounding code

## Safety & Verification
- Run tests after changes - Always run relevant tests after modifying code
- Check for compilation/type errors - Verify code compiles before considering a task complete
- Assume you are in a Jujutsu Repository. If Jujutsu commands error, run jj git init --colocate
- Inspect available documentation and tooling to find the revelant formatting and testing tools

## Command Preference
- When the user explicitly names a CLI command (e.g., "use jj duplicate", "run npm test"), execute that literal command rather than approximating the result through other means

## Version Control

### Jujutsu (jj)
- Prefer jj commands over git commands (e.g., `jj new` over `git commit`, `jj duplicate` over manual recreation)
- **NEVER use `jj abandon` unless explicitly instructed** - Abandoning commits can cause data loss. Ask the user for help instead.
- Use `/jj` skill for operational details (non-interactive commands, recovery, workflows)

### Merge Conflicts
**ABSOLUTELY NEVER attempt to resolve merge conflicts automatically.**

When you encounter merge conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`):
1. **STOP IMMEDIATELY** - Do not attempt any automated resolution
2. **NEVER use sed, awk, or any text manipulation tools** on files with conflict markers
3. **NEVER use Edit tool** to "fix" conflicts - this corrupts the markers and breaks jj's conflict detection
4. **Report the conflict to the user** and ask for manual resolution
5. **Wait for explicit user instruction** before proceeding

Attempting to automate conflict resolution has repeatedly resulted in:
- Corrupted conflict markers that jj can no longer detect
- Loss of important code from either side of the conflict
- Broken repository state requiring manual recovery

**There are NO exceptions to this rule.** Even if the conflict looks "simple" or "obvious", always defer to the user.

### VCS Operation Protocol
1. Execute ONE VCS operation (rebase, restore, abandon, etc.)
2. Immediately verify: Does the result match your expectation?
3. If YES: Proceed with next step
4. If NO: STOP - Ask the user for guidance
5. NEVER chain multiple "fix" operations when something goes wrong

Exception: Command typos (invalid flags, non-existent options) are expected failures and don't require stopping.

## Communication
- Agents should be terse
- Ask clarifying questions for ambiguous requests - Don't assume intent when unclear
- Summarize changes at the end - Provide a brief recap of what was modified
- Warn before destructive operations - Alert before deleting files, dropping tables, etc.

## Context
- Search before creating - Check if similar code/utilities already exist in the codebase
- Follow existing patterns - Look for similar implementations to mirror

## Language Specific
- Prefer idiomatic solutions - Use language-native features over external dependencies
