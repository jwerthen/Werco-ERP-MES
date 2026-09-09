import { CURRENT_GEOMETRY_PROFILE } from '../src/features/nesting/lib/geometry-profile';
/** Fixed worker entrypoint. The caller supplies data over stdin, never code. */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { once } from 'node:events';
import { calculateSavedProject, SERVER_RUN_PROFILE } from '../src/features/nesting/lib/server-run';
import { calculateRemnantProject } from '../src/features/nesting/lib/remnant-planning';
import { REMNANT_DOMAIN_PROFILE } from '../src/features/nesting/lib/remnant-domain-profile';

async function writeMessage(value: object) {
  const line = JSON.stringify(value, (_key, item: unknown) => {
    if (typeof item === 'number' && !Number.isFinite(item)) throw new Error('nonfinite_output');
    return item;
  });
  if (Buffer.byteLength(line, 'utf8') + 1 > SERVER_RUN_PROFILE.maxMessageBytes) throw new Error('output_limit');
  if (!process.stdout.write(line + '\n')) await once(process.stdout, 'drain');
}

async function main() {
  let inputSha256 = '';
  let protocol = 1;
  try {
    if (Number(process.versions.node.split('.')[0]) !== SERVER_RUN_PROFILE.nodeMajor) throw new Error('runtime_error');
    let size = 0;
    const chunks: Buffer[] = [];
    for await (const chunk of process.stdin) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += bytes.length;
      // Envelope is small and fixed; the estimate's exact 5 MiB cap is enforced
      // by the API. The parent sends only a saved immutable snapshot.
      if (size > SERVER_RUN_PROFILE.maxInputBytes + 1024) throw new Error('input_limit');
      chunks.push(bytes);
    }
    const input: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('invalid_geometry');
    const data = input as Record<string, unknown>;
    if (
      Object.keys(data).sort().join(',') !== 'estimate,input_sha256,protocol' ||
      (data.protocol !== 1 && data.protocol !== 2) ||
      typeof data.input_sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/.test(data.input_sha256)
    )
      throw new Error('invalid_geometry');
    inputSha256 = data.input_sha256;
    protocol = data.protocol;
    const hasRemnant =
      data.estimate &&
      typeof data.estimate === 'object' &&
      Object.prototype.hasOwnProperty.call(data.estimate, 'remnantPlan');
    if ((protocol === 2) !== Boolean(hasRemnant)) throw new Error('invalid_geometry');
    await writeMessage({
      type: 'hello',
      protocol,
      input_sha256: inputSha256,
      solver_version: SERVER_RUN_PROFILE.solverVersion,
      geometry_profile: CURRENT_GEOMETRY_PROFILE,
      ...(protocol === 2 ? { remnant_domain_profile: REMNANT_DOMAIN_PROFILE } : {}),
      bundle_sha256: createHash('sha256').update(readFileSync(__filename)).digest('hex'),
      node_version: process.version,
      units: 'mm',
    });
    const messages =
      protocol === 2
        ? calculateRemnantProject(data.estimate, inputSha256)
        : calculateSavedProject(data.estimate, inputSha256);
    for await (const message of messages) await writeMessage(message);
  } catch (error) {
    // Do not send private drawing content or an internal stack to stderr/logs.
    const code =
      error instanceof Error && ['output_limit', 'input_limit', 'runtime_error'].includes(error.message)
        ? error.message
        : 'invalid_geometry';
    await writeMessage({ type: 'error', protocol, input_sha256: inputSha256, code });
    process.exitCode = 2;
  }
}
void main();
