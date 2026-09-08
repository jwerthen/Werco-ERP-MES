import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

const wrapper = JSON.parse(
  await readFile(new URL('../src/features/nesting/lib/geometry-profile.generated.json', import.meta.url), 'utf8')
);
const sorted = value =>
  Array.isArray(value)
    ? value.map(sorted)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, sorted(value[key])]))
      : value;
const canonical = JSON.stringify(sorted({ id: wrapper.identity.id, profile: wrapper.profile }));
if (!/^[\x00-\x7f]*$/.test(canonical)) throw new Error('Geometry profile must be ASCII.');
const digest = createHash('sha256').update(canonical, 'ascii').digest('hex');
if (digest !== wrapper.identity.sha256) throw new Error('Generated geometry profile checksum mismatch.');
console.log(`Verified generated geometry profile ${wrapper.identity.id}: ${digest}`);
