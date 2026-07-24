#!/usr/bin/env node
/**
 * audit-check.mjs — allowlist-aware `npm audit` gate for the frontend.
 *
 * Replaces a bare `npm audit --audit-level=high`. Same hard-gate semantics:
 * ANY high/critical advisory fails the build (exit 1) — EXCEPT ids explicitly
 * listed in ./audit-allowlist.json with a written not-applicable justification.
 *
 * Design notes (see docs/SECURITY_ADVISORY_SUPPRESSIONS.md):
 *   - Zero npm dependencies. A security gate should not add supply-chain surface.
 *   - Fails CLOSED: an unparseable/failed audit, or a finding whose advisory id
 *     cannot be resolved, is treated as a failure rather than a pass.
 *   - No time-based expiry. A stale allowlist entry prints a non-fatal WARNING;
 *     it never turns CI red on a date boundary. (This gate already goes red with
 *     no code change when advisories publish; one such surprise is enough.)
 *
 * Exit codes: 0 = pass (clean, or only allowlisted findings). 1 = fail.
 */

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = join(SCRIPT_DIR, '..');
const ALLOWLIST_PATH = join(SCRIPT_DIR, 'audit-allowlist.json');

/** Severities that fail the build — matches the previous `--audit-level=high`. */
const BLOCKING_SEVERITIES = new Set(['high', 'critical']);

function fail(message) {
  console.error(`\n[audit-check] ERROR: ${message}`);
  process.exit(1);
}

/**
 * Run `npm audit --json` and return the parsed report.
 *
 * `npm audit` exits NON-ZERO whenever vulnerabilities exist, so we must capture
 * output and parse it rather than relying on the exit status.
 */
function runAudit() {
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const result = spawnSync(npm, ['audit', '--json'], {
    cwd: PROJECT_DIR,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });

  if (result.error) {
    fail(`could not run \`npm audit\`: ${result.error.message}`);
  }

  const stdout = (result.stdout || '').trim();
  if (!stdout) {
    fail(
      `\`npm audit\` produced no output (exit ${result.status}).\n` +
        `stderr:\n${(result.stderr || '').trim() || '(empty)'}`
    );
  }

  let report;
  try {
    report = JSON.parse(stdout);
  } catch (err) {
    fail(
      `could not parse \`npm audit --json\` output (exit ${result.status}): ${err.message}\n` +
        `first 500 chars:\n${stdout.slice(0, 500)}`
    );
  }

  // npm reports registry/network trouble as a JSON body with an `error` key and
  // no findings. Treat that as a gate failure, never as "no vulnerabilities".
  if (report && report.error) {
    const { code, summary } = report.error;
    fail(`\`npm audit\` failed (${code || 'unknown'}): ${summary || JSON.stringify(report.error)}`);
  }

  if (!report || typeof report.vulnerabilities !== 'object' || report.vulnerabilities === null) {
    fail(
      'unexpected `npm audit --json` shape: no `vulnerabilities` object ' +
        `(auditReportVersion=${report && report.auditReportVersion}). ` +
        'This script expects the npm 7+ (auditReportVersion 2) format.'
    );
  }

  return report;
}

/** Extract a GHSA id from an advisory `url`, falling back to the numeric source id. */
function advisoryIdOf(via) {
  const url = typeof via.url === 'string' ? via.url : '';
  const match = url.match(/(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})/i);
  if (match) return match[1];
  if (via.source !== undefined && via.source !== null) return `NPM-${via.source}`;
  return null;
}

/**
 * Resolve the concrete advisories behind one `vulnerabilities[name]` entry.
 *
 * `via[]` holds EITHER advisory objects (the real GHSA record) OR plain strings
 * naming another vulnerable package this one inherits from. The transitive case
 * is not decorative: today `react-router-dom` has `via: ["react-router"]` and
 * carries no advisory object of its own, so a parser that only reads objects
 * resolves ZERO ids for it and would report it as un-allowlisted forever.
 */
function resolveAdvisories(name, vulnerabilities, seen = new Set()) {
  if (seen.has(name)) return [];
  seen.add(name);

  const entry = vulnerabilities[name];
  if (!entry || !Array.isArray(entry.via)) return [];

  const out = [];
  for (const via of entry.via) {
    if (typeof via === 'string') {
      out.push(...resolveAdvisories(via, vulnerabilities, seen));
      continue;
    }
    if (via && typeof via === 'object') {
      const id = advisoryIdOf(via);
      if (id) {
        out.push({
          id,
          title: via.title || '(no title)',
          url: via.url || '',
          severity: (via.severity || '').toLowerCase(),
          range: via.range || '',
        });
      }
    }
  }
  return out;
}

function loadAllowlist() {
  let raw;
  try {
    raw = readFileSync(ALLOWLIST_PATH, 'utf8');
  } catch (err) {
    fail(`could not read allowlist at ${ALLOWLIST_PATH}: ${err.message}`);
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    fail(`allowlist ${ALLOWLIST_PATH} is not valid JSON: ${err.message}`);
  }

  const advisories = Array.isArray(parsed.advisories) ? parsed.advisories : [];
  const byId = new Map();
  for (const entry of advisories) {
    if (!entry || typeof entry.id !== 'string' || !entry.id.trim()) {
      fail(`allowlist entry is missing a string \`id\`: ${JSON.stringify(entry)}`);
    }
    // `reason` may be a string or an array of lines (multi-line justifications
    // stay readable in JSON that way). Normalize to an array of lines.
    const reasonLines = (Array.isArray(entry.reason) ? entry.reason : [entry.reason])
      .filter((line) => typeof line === 'string')
      .map((line) => line.trimEnd());
    if (reasonLines.join('').trim() === '') {
      fail(`allowlist entry ${entry.id} is missing a \`reason\`. Suppressions require a written justification.`);
    }
    byId.set(entry.id.toUpperCase(), { ...entry, reasonLines });
  }
  return byId;
}

function main() {
  const report = runAudit();
  const vulnerabilities = report.vulnerabilities;
  const allowlist = loadAllowlist();
  const matchedAllowlistIds = new Set();

  const blocked = [];
  const suppressed = [];

  for (const [name, entry] of Object.entries(vulnerabilities)) {
    const severity = String(entry.severity || '').toLowerCase();
    if (!BLOCKING_SEVERITIES.has(severity)) continue;

    const advisories = resolveAdvisories(name, vulnerabilities);
    // Dedupe: the same GHSA can arrive via several paths through the graph.
    const unique = new Map();
    for (const adv of advisories) if (!unique.has(adv.id)) unique.set(adv.id, adv);
    const advisoryList = [...unique.values()];

    if (advisoryList.length === 0) {
      // Fail closed: we cannot judge what we cannot identify.
      blocked.push({
        package: name,
        severity,
        advisories: [
          { id: '(unresolved)', title: 'Could not resolve an advisory id from the audit report', url: '', severity },
        ],
      });
      continue;
    }

    const notAllowed = advisoryList.filter((adv) => {
      const hit = allowlist.get(adv.id.toUpperCase());
      if (hit) {
        matchedAllowlistIds.add(adv.id.toUpperCase());
        return false;
      }
      return true;
    });

    if (notAllowed.length > 0) {
      blocked.push({ package: name, severity, advisories: notAllowed });
    } else {
      suppressed.push({ package: name, severity, advisories: advisoryList });
    }
  }

  const counts = (report.metadata && report.metadata.vulnerabilities) || {};
  console.log('[audit-check] npm audit (frontend) — blocking severities: high, critical');
  console.log(
    `[audit-check] audit totals: critical=${counts.critical ?? 0} high=${counts.high ?? 0} ` +
      `moderate=${counts.moderate ?? 0} low=${counts.low ?? 0}`
  );

  if (suppressed.length > 0) {
    // Group by advisory id: one GHSA routinely covers several packages (the
    // direct dependency plus everything that pulls it in), and repeating the
    // full justification per package buries the CI log.
    const byAdvisory = new Map();
    for (const item of suppressed) {
      for (const adv of item.advisories) {
        const key = adv.id.toUpperCase();
        if (!byAdvisory.has(key)) byAdvisory.set(key, { adv, packages: [] });
        byAdvisory.get(key).packages.push(`${item.package} (${item.severity})`);
      }
    }
    console.log(`\n[audit-check] ALLOWLISTED (${byAdvisory.size} advisory/advisories — documented as not applicable):`);
    for (const { adv, packages } of byAdvisory.values()) {
      const entry = allowlist.get(adv.id.toUpperCase());
      console.log(`  - ${adv.id}  ${adv.title}`);
      console.log(`      packages:    ${packages.join(', ')}`);
      console.log('      reason:');
      for (const line of entry.reasonLines) console.log(`        ${line}`);
      if (entry.remove_when) console.log(`      remove when: ${entry.remove_when}`);
      if (entry.reviewed) console.log(`      reviewed:    ${entry.reviewed}`);
    }
  }

  // Stale-suppression warning. Non-fatal by design — see the header note on why
  // this gate must not acquire a second surprise-failure mechanism.
  const stale = [...allowlist.keys()].filter((id) => !matchedAllowlistIds.has(id));
  if (stale.length > 0) {
    console.log(`\n[audit-check] WARNING (non-fatal): ${stale.length} allowlist entry/entries no longer match any`);
    console.log('[audit-check] current high/critical advisory. Delete them from scripts/audit-allowlist.json:');
    for (const id of stale) {
      const entry = allowlist.get(id);
      console.log(`  - ${entry.id}  (${entry.package || 'unknown package'})`);
    }
  }

  if (blocked.length > 0) {
    console.error(`\n[audit-check] FAIL: ${blocked.length} package(s) with un-allowlisted high/critical advisories:\n`);
    for (const item of blocked) {
      for (const adv of item.advisories) {
        console.error(`  ✖ ${adv.id}`);
        console.error(`      package:  ${item.package}`);
        console.error(`      severity: ${adv.severity || item.severity}`);
        console.error(`      title:    ${adv.title}`);
        if (adv.range) console.error(`      affects:  ${adv.range}`);
        if (adv.url) console.error(`      url:      ${adv.url}`);
        console.error('');
      }
    }
    console.error('[audit-check] Fix it (preferred): upgrade the dependency until `npm audit` clears the advisory.');
    console.error('[audit-check] If — and only if — the advisory is genuinely not applicable to this app, add a');
    console.error('[audit-check] documented entry to frontend/scripts/audit-allowlist.json.');
    console.error('[audit-check] See docs/SECURITY_ADVISORY_SUPPRESSIONS.md.');
    console.error('[audit-check] Do NOT run `npm audit fix --force` — it downgrades react-router-dom to 7.11.0');
    console.error('[audit-check] and reintroduces four advisories patched in 7.18.0.\n');
    process.exit(1);
  }

  console.log(
    `\n[audit-check] PASS — no un-allowlisted high/critical advisories ` +
      `(${suppressed.length} allowlisted, ${stale.length} stale allowlist entry/entries).`
  );
  process.exit(0);
}

main();
