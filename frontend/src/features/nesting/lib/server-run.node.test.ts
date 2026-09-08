import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { rect } from './nesting';
import { createBlankProject, projectToFile } from './quote-project';
import { createBlankQuote } from './quoting';

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

function execute(estimate: unknown, locale = 'C') {
  const child = spawnSync(process.execPath, ['--max-old-space-size=512', path.join(outputDir, 'solver.cjs')], {
    input: JSON.stringify({ protocol: 1, input_sha256: 'a'.repeat(64), estimate }),
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
