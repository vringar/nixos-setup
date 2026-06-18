#!/usr/bin/env node
// Validate BPMN files for Camunda 8 (Cloud) runtime compatibility using
// bpmnlint + bpmnlint-plugin-camunda-compat + zeebe-bpmn-moddle. Output mirrors
// the bpmnlint shape (one block per file), so the skill's fix loop works
// identically to lint-bpmn / lint-dmn / lint-forms.
//
// Usage:  node validate.cjs [--version <X.Y>] <file.bpmn>...
//
// Deps are provided via NODE_PATH (set by the Nix home-manager config).
// The skill ships its own .bpmnlintrc inside this dir (rewritten per --version)
// so any project-side config the user has is ignored — runtime-compat checks
// are the whole point.

const path = require('path');
const fs = require('fs');
const { spawnSync } = require('child_process');

const SKILL_DIR = __dirname;

// Parse args: --version flag (default: latest cloud config) plus file paths.
const SUPPORTED_VERSIONS = ['8.0','8.1','8.2','8.3','8.4','8.5','8.6','8.7','8.8','8.9','8.10'];
const DEFAULT_VERSION = '8.9';

let version = DEFAULT_VERSION;
const files = [];
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--version' || a === '-v') {
    version = argv[++i];
  } else if (a === '--help' || a === '-h') {
    printHelp();
    process.exit(0);
  } else {
    files.push(a);
  }
}

if (!SUPPORTED_VERSIONS.includes(version)) {
  process.stderr.write(`Unsupported version "${version}". Supported: ${SUPPORTED_VERSIONS.join(', ')}\n`);
  process.exit(2);
}
if (files.length === 0) {
  printHelp();
  process.exit(2);
}

// Resolve user paths *before* changing cwd; the user passed them relative to
// their own shell, not relative to the skill dir.
const absFiles = files.map(p => path.resolve(p));
for (const f of absFiles) {
  if (!fs.existsSync(f)) {
    process.stderr.write(`File not found: ${f}\n`);
    process.exit(2);
  }
}

// Write the per-version .bpmnlintrc in a temp dir. bpmnlint walks up from cwd
// looking for this file; we write it under a tmp path and run bpmnlint from there.
const os = require('os');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'lint-camunda-compat-'));
const cfg = {
  extends: ['bpmnlint:recommended', `plugin:camunda-compat/camunda-cloud-${version.replace('.', '-')}`],
  moddleExtensions: { zeebe: 'zeebe-bpmn-moddle/resources/zeebe.json' }
};
fs.writeFileSync(path.join(tmpDir, '.bpmnlintrc'), JSON.stringify(cfg, null, 2) + '\n');

const bin = process.env.BPMNLINT_BIN || 'bpmnlint';
const result = spawnSync(bin, absFiles, { cwd: tmpDir, stdio: 'inherit' });
fs.rmSync(tmpDir, { recursive: true, force: true });
process.exit(result.status == null ? 1 : result.status);

function printHelp() {
  process.stderr.write([
    'Usage: validate.cjs [--version X.Y] <file.bpmn>...',
    '',
    'Validates BPMN files for Camunda 8 (Cloud) runtime compatibility.',
    `Supported versions: ${SUPPORTED_VERSIONS.join(', ')} (default: ${DEFAULT_VERSION})`,
    'Camunda 7 (Platform) is not supported by this wrapper — see SKILL.md for manual config.',
    ''
  ].join('\n'));
}
