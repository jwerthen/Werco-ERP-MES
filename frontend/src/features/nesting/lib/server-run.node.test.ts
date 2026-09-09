import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { rect } from './nesting';
import { createBlankProject, projectToFile } from './quote-project';
import { createBlankQuote } from './quoting';
import { remnantPlanningFixture } from '../../../test-utils/remnantPlanningFixtures';
import { buildRemnantPlan } from './remnant-evidence';
import { REMNANT_DOMAIN_PROFILE } from './remnant-domain-profile';

let outputDir: string;
beforeAll(() => {
  outputDir = mkdtempSync(path.join(os.tmpdir(), 'werco-nest-node-test-'));
  const build = spawnSync(process.execPath, ['tools/build-nesting-worker.mjs', `--out-dir=${outputDir}`], {
    cwd: process.cwd(),
    encoding: 'utf8',
    timeout: 15000,
  });
  expect(build.status).toBe(0);
}, 20000);
afterAll(() => {
  if (outputDir) rmSync(outputDir, { recursive: true, force: true });
});

function execute(estimate: unknown, locale = 'C', protocol = 1) {
  const child = spawnSync(process.execPath, ['--max-old-space-size=512', path.join(outputDir, 'solver.cjs')], {
    input: JSON.stringify({ protocol, input_sha256: 'a'.repeat(64), estimate }),
    encoding: 'utf8',
    timeout: 10000,
    maxBuffer: 8 * 1024 * 1024,
    env: { LANG: locale, TZ: 'UTC' },
  });
  return {
    child,
    messages: child.stdout
      .trim()
      .split('\n')
      .filter(Boolean)
      .map(line => JSON.parse(line)),
  };
}
function fixture() {
  return projectToFile(
    createBlankProject({
      ...createBlankQuote(),
      parts: [{ id: 'plate', name: 'Synthetic plate', quantity: 2, rotate: true, color: 0, loops: [rect(25.4, 50.8)] }],
      options: [{ id: 'stock', enabled: true, price: null, width: 100, height: 100 }],
    })
  );
}

it('executes the bundled worker with no app credentials and ties hello to the packaged bytes', () => {
  const { child, messages } = execute(fixture());
  expect(child.status).toBe(0);
  expect(child.stderr).toBe('');
  const manifest = JSON.parse(readFileSync(path.join(outputDir, 'manifest.json'), 'utf8'));
  const digest = createHash('sha256')
    .update(readFileSync(path.join(outputDir, 'solver.cjs')))
    .digest('hex');
  expect(manifest.bundle_sha256).toBe(digest);
  expect(messages[0]).toMatchObject({
    type: 'hello',
    protocol: manifest.protocol,
    solver_version: manifest.solver_version,
    node_version: process.version,
    bundle_sha256: digest,
    units: 'mm',
    input_sha256: 'a'.repeat(64),
  });
  expect(messages[1]).toMatchObject({ type: 'option', sequence: 1, requested: 2, result: { complete: true } });
  expect(messages[2]).toMatchObject({
    type: 'summary',
    evaluated_count: 1,
    total_options: 1,
    stop_reason: 'completed',
  });
  expect(execute(fixture(), 'de_DE.UTF-8').messages).toEqual(messages);
});

it('fails invalid source units without emitting an option or exposing drawing content', () => {
  const { child, messages } = execute({ ...fixture(), units: 'mm' });
  expect(child.status).toBe(2);
  expect(messages.some(message => message.type === 'option')).toBe(false);
  expect(messages[messages.length - 1]).toEqual({
    type: 'error',
    protocol: 1,
    input_sha256: 'a'.repeat(64),
    code: 'invalid_geometry',
  });
  expect(child.stderr).toBe('');
});

it('executes the same bundled protocol2 without credentials and preserves staged original-instance accounting', async () => {
  const f = await remnantPlanningFixture();
  const remnantPlan = await buildRemnantPlan({
    resolution: f.resolution,
    companyId: 2,
    groupId: f.groupId,
    quote: f.quote,
    family: 'Carbon steel',
    requiredGrade: 'A36',
    reason: 'Synthetic conditional comparison',
    zoneClearanceIn: '0.375',
  });
  const estimate = {
    version: 18,
    units: 'in',
    currency: 'USD',
    name: 'Recorded source',
    activeGroupId: f.groupId,
    groups: [{ id: f.groupId, quote: f.quote }],
    remnantPlan,
  };
  const { child, messages } = execute(estimate, 'C', 2);
  expect(child.status).toBe(0);
  expect(child.stderr).toBe('');
  const manifest = JSON.parse(readFileSync(path.join(outputDir, 'manifest.json'), 'utf8'));
  expect(manifest).toMatchObject({
    protocol: 1,
    supported_protocols: [1, 2],
    remnant_domain_profile: REMNANT_DOMAIN_PROFILE,
  });
  expect(messages[0]).toMatchObject({
    type: 'hello',
    protocol: 2,
    remnant_domain_profile: REMNANT_DOMAIN_PROFILE,
    solver_version: 'werco-contour-v7',
    bundle_sha256: manifest.bundle_sha256,
  });
  const piece = messages.find(m => m.stage_kind === 'recorded_piece');
  expect(piece).toMatchObject({
    stock: { domain: { sourceOriginIn: { x: '-0.000000001', y: '0' } } },
    result: {
      complete: true,
      nest: { sheets: 1, placements: expect.any(Array) },
      leftovers: { version: 'werco-leftovers-v4' },
    },
  });
  expect(piece.result.nest.placements).toHaveLength(2);
  expect(messages.filter(m => m.stage_kind === 'residual')).toHaveLength(3);
  expect(
    messages
      .filter(m => m.stage_kind === 'residual')
      .every(m => m.requested === 0 && m.stock === null && m.result === null)
  ).toBe(true);
  expect(messages[messages.length - 1]).toMatchObject({
    type: 'summary',
    protocol: 2,
    evaluated_count: 7,
    complete_option_count: 6,
  });
  expect(execute(estimate, 'de_DE.UTF-8', 2).messages).toEqual(messages);
  const wrongProtocol = execute(estimate);
  expect(wrongProtocol.child.status).toBe(2);
  expect(wrongProtocol.messages.some(m => m.type === 'option' || m.type === 'stage')).toBe(false);
  expect(execute(fixture(), 'C', 2).child.status).toBe(2);
});
