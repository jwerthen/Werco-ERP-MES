import { checkSourceIntent, checkSourcePage, fingerprintOriginal, savedSourceParts } from './cadSourceEvidence';
import { originalFile, sourceFixture, sourceIntent, sourcePage } from '../../test-utils/nestingSourceFixtures';
import { sha256 } from './lib/provenance';

test('raw byte fingerprint preserves BOM, CRLF and non-ASCII rather than decoded DXF text', async () => {
  const file = originalFile();
  const raw = await file.arrayBuffer();
  expect(await fingerprintOriginal(file)).toBe(await sha256(raw));
  expect(await fingerprintOriginal(file)).not.toBe(await sha256(new TextDecoder().decode(raw).replace(/\r/g, '')));
  await expect(fingerprintOriginal(originalFile(''))).rejects.toThrow('contain bytes');
  const large = originalFile('a');
  Object.defineProperty(large, 'size', { value: 5_000_000 });
  await expect(fingerprintOriginal(large)).rejects.toThrow('smaller');
  expect(large.arrayBuffer).not.toHaveBeenCalled();
});

test('one original binds multiple groups/profiles with exact reported revision evidence', async () => {
  const { revision, parts, request } = await sourceFixture();
  const intent = await sourceIntent(request, parts, true);
  expect((await checkSourceIntent(intent, revision, parts, request)).target_count).toBe(2);
  expect(intent.targets[0].provenance.reportedRevision).toBe('A');
  expect(intent.targets[1].provenance).not.toHaveProperty('reportedRevision');
  expect((await checkSourcePage(sourcePage([intent]), revision, parts)).items).toEqual([intent]);
});

test.each(['company_id', 'draft_id', 'revision_number'] as const)(
  'refuses a different %s in attachment history',
  async key => {
    const { revision, parts, request } = await sourceFixture();
    const intent = await sourceIntent(request, parts);
    await expect(checkSourceIntent({ ...intent, [key]: 999 }, revision, parts)).rejects.toThrow();
  }
);

test('refuses metadata, target, digest, and receipt tampering', async () => {
  const { revision, parts, request } = await sourceFixture();
  const intent = await sourceIntent(request, parts, true);
  await expect(checkSourceIntent({ ...intent, source_name: 'other.dxf' }, revision, parts, request)).rejects.toThrow(
    'submitted'
  );
  await expect(checkSourceIntent({ ...intent, targets_sha256: 'd'.repeat(64) }, revision, parts)).rejects.toThrow(
    'fingerprint'
  );
  await expect(
    checkSourceIntent({ ...intent, targets: [intent.targets[0], intent.targets[0]] }, revision, parts)
  ).rejects.toThrow('revision');
  await expect(
    checkSourceIntent({ ...intent, receipt: { ...intent.receipt, byte_count: 99 } }, revision, parts)
  ).rejects.toThrow('inconsistent');
  await expect(
    checkSourceIntent(
      {
        ...intent,
        targets: intent.targets.map(t => ({ ...t, provenance: { ...t.provenance, reportedRevision: 'WRONG' } })),
      },
      revision,
      parts
    )
  ).rejects.toThrow('provenance');
});

test('read metadata preserves historical null revision and refuses text-hash eligibility', async () => {
  const { revision, parts, request } = await sourceFixture();
  const copy = JSON.parse(JSON.stringify(revision));
  copy.estimate.groups[0].quote.parts[0].revision = null;
  expect(savedSourceParts(copy, revision)[0].revision).toBeNull();
  const textParts = parts.map(p => ({
    ...p,
    provenance: p.provenance ? { ...p.provenance, sourceHashBasis: 'utf8-text' as const } : undefined,
  }));
  await expect(checkSourceIntent(await sourceIntent(request, parts), revision, textParts)).rejects.toThrow(
    'original-byte'
  );
});

test('completion cannot change immutable intent/actor identity or replace a historical receipt', async () => {
  const { revision, parts, request } = await sourceFixture();
  const pending = await sourceIntent(request, parts);
  const complete = await sourceIntent(request, parts, true);
  expect(await checkSourceIntent(complete, revision, parts, undefined, pending)).toEqual(complete);
  await expect(
    checkSourceIntent(
      { ...complete, created_by: 9, receipt: { ...complete.receipt, created_by: 9 } },
      revision,
      parts,
      undefined,
      pending
    )
  ).rejects.toThrow('immutable');
  await expect(
    checkSourceIntent({ ...complete, receipt: { ...complete.receipt, id: 999 } }, revision, parts, undefined, complete)
  ).rejects.toThrow('immutable');
});
