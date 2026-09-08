import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CADSourceAttachments from './CADSourceAttachments';
import TeamDrafts from './TeamDrafts';
import { NestingPortalContext } from './PortalContext';
import { savedSourceParts } from './cadSourceEvidence';
import { createBlankProject } from './lib/quote-project';
import { sha256 } from './lib/provenance';
import api from '../../services/api';
import { originalFile, sourceFixture, sourceIntent, sourcePage } from '../../test-utils/nestingSourceFixtures';
import type { NestingSourceIntent } from '../../types/nestingSource';

let mockCompanyId = 2;
let mockUserId = 7;
// Real pacing has a separate fake-clock test. This suite checks data/ordering
// across 100 requests with the actual hashing and receipt validators active.
jest.mock('./cadSourceTransport', () => ({
  ...jest.requireActual('./cadSourceTransport'),
  createSourcePacer: () => async () => undefined,
}));
jest.mock('../../context/AuthContext', () => ({ useAuth: () => ({ user: { id: mockUserId } }) }));
jest.mock('../../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: mockCompanyId } }) }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingDraftRevision: jest.fn(),
    listNestingSources: jest.fn(),
    createNestingSourceIntent: jest.fn(),
    uploadNestingSource: jest.fn(),
    finalizeNestingSource: jest.fn(),
    downloadNestingSource: jest.fn(),
    listNestingDrafts: jest.fn(),
    listNestingDraftRevisions: jest.fn(),
    saveNestingDraft: jest.fn(),
  },
}));

const get = jest.mocked(api.getNestingDraftRevision);
const list = jest.mocked(api.listNestingSources);
const create = jest.mocked(api.createNestingSourceIntent);
const upload = jest.mocked(api.uploadNestingSource);
const finalize = jest.mocked(api.finalizeNestingSource);
const download = jest.mocked(api.downloadNestingSource);
const label = 'Choose original DXFs (up to 100; each smaller than 5 MB)';
const retained = 'Original bytes retained. Geometry and reported revision remain unapproved.';
const onBack = jest.fn();
const guard = jest.fn();
let fixture: Awaited<ReturnType<typeof sourceFixture>>;

function completed(pending: NestingSourceIntent): NestingSourceIntent {
  return {
    ...pending,
    state: 'ATTACHED',
    can_resume: false,
    attempt_count: 1,
    receipt: {
      id: pending.id + 1000,
      source_sha256: pending.source_sha256,
      byte_count: pending.byte_count,
      verified_at: '2026-09-08T18:06:00Z',
      created_by: pending.created_by,
      submitted_api_token_id: pending.submitted_api_token_id,
      claim: 'server_hash_verified_unapproved',
    },
  };
}

beforeEach(async () => {
  jest.resetAllMocks();
  mockCompanyId = 2;
  mockUserId = 7;
  fixture = await sourceFixture();
  get.mockResolvedValue(fixture.revision);
  list.mockResolvedValue(sourcePage());
  create.mockImplementation(async (_draft, _number, request) => sourceIntent(request, fixture.parts));
  upload.mockImplementation(async () => completed(await sourceIntent(create.mock.calls[0][2], fixture.parts)));
});
afterEach(() => jest.restoreAllMocks());

function mount() {
  return render(<CADSourceAttachments target={fixture.revision} onBack={onBack} registerCloseGuard={guard} />);
}
async function select(files = [fixture.file]) {
  fireEvent.change(await screen.findByLabelText(label), { target: { files } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Attach selected originals' })).toBeEnabled());
}

test('100 distinct originals produce 100 byte-exact sequential attachments with unique frozen commands', async () => {
  const files = Array.from({ length: 100 }, (_, index) =>
    originalFile(`\uFEFF0\r\nSECTION\r\n999\r\nSynthetic Café ${index}\r\n0\r\nEOF\r\n`, `source-${index}.dxf`)
  );
  const expectedBytes = await Promise.all(files.map(file => file.arrayBuffer()));
  const hashes = await Promise.all(expectedBytes.map(bytes => sha256(bytes)));
  fixture.revision = {
    ...fixture.revision,
    estimate: {
      version: 15,
      groups: [
        {
          id: 'batch',
          quote: {
            name: 'Synthetic original batch',
            material: 'Carbon steel',
            parts: files.map((file, index) => ({
              id: `part-${index}`,
              name: `Profile ${index}`,
              provenance: { ...fixture.parts[0].provenance!, sourceName: file.name, sourceSha256: hashes[index] },
            })),
          },
        },
      ],
    },
  };
  fixture.parts = savedSourceParts(fixture.revision, fixture.revision);
  get.mockResolvedValue(fixture.revision);
  const pending = new Map<number, NestingSourceIntent>();
  const order: string[] = [];
  let active = 0;
  let maxActive = 0;
  create.mockImplementation(async (_draft, _number, request) => {
    const index = hashes.indexOf(request.source_sha256);
    order.push(`intent:${index}`);
    const result = { ...(await sourceIntent(request, fixture.parts)), id: index + 51 };
    pending.set(result.id, result);
    return result;
  });
  upload.mockImplementation(async (_draft, _number, id, _company, bytes) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    const index = id - 51;
    expect(new Uint8Array(bytes)).toEqual(new Uint8Array(expectedBytes[index]));
    order.push(`bytes:${index}`);
    await Promise.resolve();
    active -= 1;
    return completed(pending.get(id)!);
  });
  files.forEach(file => jest.mocked(file.arrayBuffer).mockClear());
  mount();
  await select(files);
  expect(screen.getAllByRole('checkbox')).toHaveLength(100);
  expect(create).not.toHaveBeenCalled();
  const attach = screen.getByRole('button', { name: 'Attach selected originals' });
  fireEvent.click(attach);
  fireEvent.click(attach);
  await screen.findByText('Batch finished. Review each file’s receipt or recovery message.', {}, { timeout: 15000 });
  expect(screen.getAllByText(retained)).toHaveLength(100);
  expect(create).toHaveBeenCalledTimes(100);
  expect(upload).toHaveBeenCalledTimes(100);
  expect(maxActive).toBe(1);
  expect(order).toEqual(files.flatMap((_, index) => [`intent:${index}`, `bytes:${index}`]));
  expect(new Set(create.mock.calls.map(call => call[2].request_key)).size).toBe(100);
  create.mock.calls.forEach(([draft, number, request], index) => {
    expect([draft, number, request.expected_company_id]).toEqual([41, 1, 2]);
    expect(request.expected_input_sha256).toBe(fixture.revision.content_sha256);
    expect(request.source_sha256).toBe(hashes[index]);
    expect(request.byte_count).toBe(expectedBytes[index].byteLength);
    expect(request.targets).toEqual([{ group_id: 'batch', part_id: `part-${index}` }]);
    expect(files[index].arrayBuffer).toHaveBeenCalledTimes(2);
  });
  expect(finalize).not.toHaveBeenCalled();
}, 30000);

test('a rejected foreign-actor intent is never used as a recovery handle', async () => {
  create.mockImplementationOnce(async (_draft, _number, request) => ({
    ...(await sourceIntent(request, fixture.parts)),
    created_by: 99,
  }));
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await screen.findByText('The upload intent belongs to a different estimator.');
  expect(upload).not.toHaveBeenCalled();
  finalize.mockRejectedValue(new Error('Still unconfirmed'));
  fireEvent.click(screen.getByRole('button', { name: 'Check attachment' }));
  await screen.findByText('Still unconfirmed');
  expect(create).toHaveBeenCalledTimes(2);
  expect(create.mock.calls[1][2]).toEqual(create.mock.calls[0][2]);
  // This finalize is for the newly checked, current-actor intent, not the
  // initially rejected response. The upload still needs a separate action.
  expect(finalize).toHaveBeenCalledTimes(1);
  expect(upload).not.toHaveBeenCalled();
  expect(screen.queryByText(retained)).not.toBeInTheDocument();
});

test.each(['credential', 'hash'] as const)('a mismatched completed receipt %s is never shown retained', async kind => {
  upload.mockImplementationOnce(async () => {
    const result = completed(await sourceIntent(create.mock.calls[0][2], fixture.parts));
    return {
      ...result,
      receipt: {
        ...result.receipt!,
        ...(kind === 'credential' ? { submitted_api_token_id: 99 } : { source_sha256: 'f'.repeat(64) }),
      },
    };
  });
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await screen.findByText('The retained-byte receipt is inconsistent with this attachment.');
  expect(screen.queryByText(retained)).not.toBeInTheDocument();
  expect(upload).toHaveBeenCalledTimes(1);
  expect(finalize).not.toHaveBeenCalled();
});

test('stopping a batch lets the submitted file finish but never starts the next file', async () => {
  const second = originalFile('Synthetic distinct original', 'second.dxf');
  const secondHash = await sha256(await second.arrayBuffer());
  const revision = { ...fixture.revision };
  revision.estimate = {
    groups: [
      {
        id: 'batch',
        quote: {
          name: 'Batch',
          material: 'Carbon steel',
          parts: [fixture.file, second].map((file, index) => ({
            id: `part-${index}`,
            name: `Profile ${index}`,
            provenance: {
              ...fixture.parts[0].provenance!,
              sourceName: file.name,
              sourceSha256: index === 0 ? fixture.request.source_sha256 : secondHash,
            },
          })),
        },
      },
    ],
  };
  fixture.revision = revision;
  fixture.parts = savedSourceParts(revision, revision);
  get.mockResolvedValue(revision);
  let resolveUpload!: (value: NestingSourceIntent) => void;
  upload.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        resolveUpload = resolve;
      })
  );
  mount();
  await select([fixture.file, second]);
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('button', { name: 'Stop after this file' }));
  await act(async () => resolveUpload(completed(await sourceIntent(create.mock.calls[0][2], fixture.parts))));
  await screen.findByText(retained);
  expect(create).toHaveBeenCalledTimes(1);
  expect(upload).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Attach selected originals' })).toBeEnabled();
});

test('a late completion after actor switch cannot update the new actor panel', async () => {
  let resolveUpload!: (value: NestingSourceIntent) => void;
  upload.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        resolveUpload = resolve;
      })
  );
  const view = mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
  const signal = upload.mock.calls[0][5];
  mockUserId = 8;
  view.rerender(<CADSourceAttachments target={fixture.revision} onBack={onBack} registerCloseGuard={guard} />);
  expect(signal?.aborted).toBe(true);
  await act(async () => resolveUpload(completed(await sourceIntent(create.mock.calls[0][2], fixture.parts))));
  expect(screen.queryByText(retained)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Attach selected originals' })).not.toBeInTheDocument();
});

test('corrupt downloaded bytes never create a browser download', async () => {
  const attached = completed(await sourceIntent(fixture.request, fixture.parts));
  list.mockResolvedValue(sourcePage([attached]));
  const wrong = new Uint8Array(attached.byte_count).fill(120);
  const blob = new Blob([wrong]);
  Object.defineProperty(blob, 'arrayBuffer', { value: async () => wrong.buffer });
  download.mockResolvedValue(blob);
  const createURL = jest.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic');
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Download original' }));
  await screen.findByText('Downloaded bytes differ from the retained receipt. Nothing was downloaded.');
  expect(createURL).not.toHaveBeenCalled();
});

test('Team drafts mounts attachments only on an exact historical-row action and never replaces unsaved inputs', async () => {
  const current = createBlankProject();
  current.name = 'Unsaved current material plan';
  const onOpen = jest.fn();
  const onSaved = jest.fn();
  jest.mocked(api.listNestingDrafts).mockResolvedValue({
    schema_version: 1,
    items: [fixture.revision],
    total: 1,
    page: 1,
    per_page: 10,
  });
  render(
    <TeamDrafts companyId={2} project={current} canSave disabled={false} dirty onOpen={onOpen} onSaved={onSaved} />,
    {
      wrapper: ({ children }) => (
        <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider>
      ),
    }
  );
  expect(get).not.toHaveBeenCalled();
  expect(list).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Team drafts' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Original DXFs for Saved nest revision 1' }));
  await screen.findByRole('region', { name: 'Original DXF attachments' });
  await screen.findByLabelText(label);
  expect(get).toHaveBeenCalledWith(41, 1, expect.any(AbortSignal));
  expect(list).toHaveBeenCalledWith(41, 1, 1, expect.any(AbortSignal));
  expect(onOpen).not.toHaveBeenCalled();
  expect(onSaved).not.toHaveBeenCalled();
  expect(api.saveNestingDraft).not.toHaveBeenCalled();
  expect(create).not.toHaveBeenCalled();
  expect(current.name).toBe('Unsaved current material plan');
});
