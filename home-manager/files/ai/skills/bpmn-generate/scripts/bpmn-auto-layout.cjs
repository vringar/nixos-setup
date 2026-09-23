#!/usr/bin/env node
'use strict';

/**
 * BPMN auto-layout — generic element + edge layout.
 *
 * Wraps the upstream `bpmn-auto-layout` library
 * (https://github.com/bpmn-io/bpmn-auto-layout) to regenerate the BPMN DI
 * section: positions every flow element and routes every sequence flow.
 *
 * Usage:
 *   node bpmn-auto-layout.cjs <file.bpmn>
 *
 * `bpmn-auto-layout` is provided via NODE_PATH set by the Nix home-manager
 * config; no npm install step is needed.
 */

const fs = require('node:fs');
const path = require('node:path');

const PKG_NAME = 'bpmn-auto-layout';

const file = process.argv[2];
if (!file) {
  console.error('Usage: bpmn-auto-layout.cjs <file.bpmn>');
  process.exit(2);
}
if (!fs.existsSync(file)) {
  console.error(`File not found: ${file}`);
  process.exit(2);
}

const ADHOC_SUBPROCESS_RE = /<([a-zA-Z0-9]+:)?adHocSubProcess\b/;

(async () => {
  const xml = fs.readFileSync(file, 'utf8');
  if (ADHOC_SUBPROCESS_RE.test(xml)) {
    console.error(
      `bpmn-auto-layout: ${path.basename(file)} — refusing to run: process contains an ad-hoc sub-process, whose layout bpmn-auto-layout does not preserve correctly`
    );
    process.exit(1);
  }
  const { layoutProcess } = require(PKG_NAME);
  const out = await layoutProcess(xml);
  fs.writeFileSync(file, out);
  console.log(`bpmn-auto-layout: ${path.basename(file)} — element & edge layout regenerated`);
})().catch(err => {
  console.error(`bpmn-auto-layout: ${path.basename(file)} — failed: ${err.message}`);
  process.exit(1);
});
