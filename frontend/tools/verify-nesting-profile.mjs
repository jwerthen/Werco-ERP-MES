import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

// Frontend-only Railway builds contain these copies, without the backend source.
const profiles = [
  ['werco-compensated-v1', '../src/features/nesting/lib/geometry-profile.generated.json'],
  ['werco-remnant-domain-v1', '../src/features/nesting/lib/remnant-domain-profile.generated.json'],
];
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value, keys) => record(value) && Object.keys(value).sort().join(',') === keys;
const sorted = value =>
  Array.isArray(value)
    ? value.map(sorted)
    : record(value)
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map(key => [key, sorted(value[key])])
        )
      : value;

async function checkedProfile(id, filename) {
  const bytes = await readFile(new URL(filename, import.meta.url));
  if (bytes.length > 16384 || bytes.some(byte => byte > 127))
    throw new Error('Generated profile requires bounded ASCII.');
  const raw = bytes.toString('ascii');
  const wrapper = JSON.parse(raw);
  if (
    !exactKeys(wrapper, 'identity,profile') ||
    !exactKeys(wrapper.identity, 'id,sha256') ||
    wrapper.identity.id !== id ||
    !/^[a-f0-9]{64}$/.test(wrapper.identity.sha256) ||
    !record(wrapper.profile)
  )
    throw new Error('Generated geometry profile identity is invalid.');
  const stack = [wrapper.profile];
  while (stack.length) {
    const value = stack.pop();
    if (Array.isArray(value)) stack.push(...value);
    else if (record(value)) {
      if (Object.keys(value).some(key => !/^[\x00-\x7f]*$/.test(key))) throw new Error('Profile keys require ASCII.');
      stack.push(...Object.values(value));
    } else if (typeof value === 'string') {
      if (!/^[\x00-\x7f]*$/.test(value)) throw new Error('Profile strings require ASCII.');
    } else if (!Number.isSafeInteger(value)) {
      throw new Error('Profile constants require decimal strings or safe integers.');
    }
  }
  // Generated formatting is deterministic. This also refuses duplicate JSON keys,
  // which JSON.parse alone would silently discard before checksum verification.
  if (raw !== JSON.stringify(sorted(wrapper), null, 2) + '\n') throw new Error('Generated profile is not canonical.');
  const canonical = JSON.stringify(sorted({ id: wrapper.identity.id, profile: wrapper.profile }));
  const digest = createHash('sha256').update(canonical, 'ascii').digest('hex');
  if (digest !== wrapper.identity.sha256) throw new Error('Generated geometry profile checksum mismatch.');
  return wrapper;
}

const wrappers = await Promise.all(profiles.map(([id, filename]) => checkedProfile(id, filename)));
if (JSON.stringify(sorted(wrappers[1].profile.compensatedProfile)) !== JSON.stringify(sorted(wrappers[0].identity))) {
  throw new Error('Remnant domain profile does not bind the exact compensated profile.');
}
for (const wrapper of wrappers) {
  console.log(`Verified generated geometry profile ${wrapper.identity.id}: ${wrapper.identity.sha256}`);
}
