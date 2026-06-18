#!/usr/bin/env node
// Validate Camunda Form (.form) files against the @bpmn-io/form-json-schema
// (vendored locally as schema.json). Output mirrors the lint-bpmn / lint-dmn
// shape: file → indented per-error lines → trailing summary. Exit 0 when clean,
// 1 when any file has at least one error.
//
// Usage: node validate.cjs <file.form>...
//
// Deps (ajv, ajv-formats) are provided via NODE_PATH set by the Nix
// home-manager config; no npm install step is needed.

const path = require('path');
const fs = require('fs');

const SCRIPT_DIR = __dirname;

const Ajv = require('ajv');
const addFormats = require('ajv-formats');

const schemaPath = path.join(SCRIPT_DIR, 'schema.json');
const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf8'));

const ajv = new Ajv.default ? new Ajv.default({ allErrors: true, strict: false }) : new Ajv({ allErrors: true, strict: false });
(addFormats.default || addFormats)(ajv);
const validate = ajv.compile(schema);

const files = process.argv.slice(2);
if (files.length === 0) {
  process.stderr.write('Usage: validate.cjs <file.form>...\n');
  process.exit(2);
}

let totalErrors = 0;

for (const file of files) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    console.log(file);
    console.log(`  /  error  ${e.message}  invalid-json`);
    console.log('');
    totalErrors++;
    continue;
  }

  const ok = validate(data);
  if (ok) continue;

  // Filter out parent if/then "must match then schema" errors when a leaf
  // error already names the same instance path or a deeper one — they're
  // structural duplicates of the real cause.
  const errors = validate.errors || [];
  const leaves = errors.filter(e => {
    if (e.keyword !== 'if') return true;
    const hasDeeper = errors.some(o =>
      o !== e &&
      o.instancePath !== e.instancePath &&
      o.instancePath.startsWith(e.instancePath)
    );
    return !hasDeeper;
  });

  if (leaves.length === 0) continue;

  console.log(file);
  for (const err of leaves) {
    const where = err.instancePath || '/';
    const keyword = err.keyword || 'invalid';
    const msg = humanise(err, data);
    console.log(`  ${where}  error  ${msg}  ${keyword}`);
    totalErrors++;
  }
  console.log('');
}

if (totalErrors > 0) {
  console.log(`✖ ${totalErrors} problem${totalErrors === 1 ? '' : 's'} (${totalErrors} error${totalErrors === 1 ? '' : 's'}, 0 warnings)`);
  process.exit(1);
}
process.exit(0);

function humanise(err, data) {
  const p = err.params || {};
  switch (err.keyword) {
    case 'required':
      return `missing required property "${p.missingProperty}"`;
    case 'additionalProperties':
      return `unexpected property "${p.additionalProperty}" — not allowed here`;
    case 'type': {
      const expected = Array.isArray(p.type) ? p.type.join(' or ') : p.type;
      return `wrong type — expected ${expected}`;
    }
    case 'enum': {
      const allowed = Array.isArray(p.allowedValues) ? p.allowedValues : [];
      const preview = allowed.length > 8 ? allowed.slice(0, 8).map(v => JSON.stringify(v)).join(', ') + `, … (${allowed.length - 8} more)` : allowed.map(v => JSON.stringify(v)).join(', ');
      return `value not allowed — must be one of [${preview}]`;
    }
    case 'const':
      return `value must be ${JSON.stringify(p.allowedValue)}`;
    case 'pattern':
      return `value does not match required pattern /${p.pattern}/`;
    case 'format':
      return `value is not a valid ${p.format}`;
    case 'minLength':
      return p.limit === 1 ? `value must not be empty` : `value too short — minimum length ${p.limit}`;
    case 'maxLength':
      return `value too long — maximum length ${p.limit}`;
    case 'minimum':
      return `value below minimum — must be ${p.comparison || '>='} ${p.limit}`;
    case 'maximum':
      return `value above maximum — must be ${p.comparison || '<='} ${p.limit}`;
    case 'exclusiveMinimum':
      return `value must be > ${p.limit}`;
    case 'exclusiveMaximum':
      return `value must be < ${p.limit}`;
    case 'multipleOf':
      return `value must be a multiple of ${p.multipleOf}`;
    case 'minItems':
      return p.limit === 1 ? `array must not be empty` : `array too short — minimum ${p.limit} items`;
    case 'maxItems':
      return `array too long — maximum ${p.limit} items`;
    case 'uniqueItems':
      return `array contains duplicate items at indices ${p.i} and ${p.j}`;
    case 'minProperties':
      return `object has too few properties — minimum ${p.limit}`;
    case 'maxProperties':
      return `object has too many properties — maximum ${p.limit}`;
    case 'oneOf':
      return Array.isArray(p.passingSchemas)
        ? `matches multiple alternative schemas (must match exactly one)`
        : `does not match any of the allowed alternatives at this location`;
    case 'anyOf':
      return `does not match any of the allowed alternatives at this location`;
    case 'allOf':
      return `does not satisfy all combined schemas — see leaf errors at deeper paths`;
    case 'not':
      return `value is forbidden by a "not" schema — remove or change it`;
    case 'if':
      return p.failingKeyword
        ? `conditional branch "${p.failingKeyword}" failed`
        : `conditional branch failed — properties are inconsistent`;
    case 'dependencies':
    case 'dependentRequired':
      return `property "${p.property}" requires "${p.missingProperty}" to also be set`;
    case 'discriminator':
      return `polymorphic discriminator "${p.tag}" mismatch`;
    case 'false schema': {
      const segs = (err.instancePath || '').split('/').filter(Boolean).map(decodePtrSeg);
      const prop = segs[segs.length - 1];
      const parent = getValueAt(data, segs.slice(0, -1));
      const ctxType = (parent && typeof parent === 'object') ? parent.type : null;
      if (prop && ctxType) return `property "${prop}" is not allowed for type "${ctxType}"`;
      if (prop) return `property "${prop}" is not allowed at this location`;
      return `value is not allowed at this location`;
    }
    default:
      return err.message || 'invalid';
  }
}

function decodePtrSeg(seg) {
  return seg.replace(/~1/g, '/').replace(/~0/g, '~');
}

function getValueAt(root, segs) {
  let cur = root;
  for (const seg of segs) {
    if (cur == null) return null;
    if (Array.isArray(cur)) {
      const idx = parseInt(seg, 10);
      if (Number.isNaN(idx)) return null;
      cur = cur[idx];
    } else if (typeof cur === 'object') {
      cur = cur[seg];
    } else {
      return null;
    }
  }
  return cur;
}
