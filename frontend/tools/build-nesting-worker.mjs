import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
if (args.length && (args.length !== 1 || !args[0].startsWith('--out-dir='))) {
  throw new Error('Usage: npm run build:nesting-worker -- --out-dir=<build-output-directory>');
}
const outputDir = path.resolve(frontendRoot, args[0]?.slice('--out-dir='.length) || '../backend/nesting-runtime');
await mkdir(outputDir, { recursive: true });
const outfile = path.join(outputDir, 'solver.cjs');
await build({
  absWorkingDir: frontendRoot,
  entryPoints: ['tools/nesting-server-worker.ts'],
  outfile,
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
  sourcemap: false,
  legalComments: 'inline',
  logLevel: 'warning',
});
const bundle = await readFile(outfile);
// Keep this protocol identity aligned with SERVER_RUN_PROFILE; the Node smoke
// test compares the build manifest to the actual executable's hello message.
const manifest = {
  protocol: 1,
  solver_version: 'werco-contour-v4',
  bundle_sha256: createHash('sha256').update(bundle).digest('hex'),
  node_major: 22,
  max_option_evaluations: 36,
  entrypoint: 'solver.cjs',
};
await writeFile(path.join(outputDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`Built nesting worker (${bundle.length} bytes); SHA256 ${manifest.bundle_sha256}`);
