import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CADSourceAttachments from './CADSourceAttachments';
import api from '../../services/api';
import { originalFile, sourceFixture, sourceIntent, sourcePage } from '../../test-utils/nestingSourceFixtures';

let mockCompanyId = 2;
let mockUserId = 7;
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
  },
}));
const get = jest.mocked(api.getNestingDraftRevision);
const list = jest.mocked(api.listNestingSources);
const create = jest.mocked(api.createNestingSourceIntent);
const upload = jest.mocked(api.uploadNestingSource);
const finalize = jest.mocked(api.finalizeNestingSource);
let fixture: Awaited<ReturnType<typeof sourceFixture>>;
const onBack = jest.fn();
const guard = jest.fn();
const fileLabel = 'Choose original DXFs (up to 100; each smaller than 5 MB)';

beforeEach(async () => {
  jest.clearAllMocks();
  mockCompanyId = 2;
  mockUserId = 7;
  fixture = await sourceFixture();
  get.mockResolvedValue(fixture.revision);
  list.mockResolvedValue(sourcePage());
  create.mockImplementation(async (_draft, _rev, request) => sourceIntent(request, fixture.parts));
  upload.mockImplementation(async () =>
    sourceIntent(create.mock.calls[0]?.[2] ?? fixture.request, fixture.parts, true)
  );
  finalize.mockImplementation(async () =>
    sourceIntent(create.mock.calls[0]?.[2] ?? fixture.request, fixture.parts, true)
  );
});
function mount() {
  return render(<CADSourceAttachments target={fixture.revision} onBack={onBack} registerCloseGuard={guard} />);
}
async function select(files = [fixture.file]) {
  fireEvent.change(await screen.findByLabelText(fileLabel), { target: { files } });
  await screen.findByRole('checkbox', { name: /Outer plate/ });
}

test('viewing and local matching do not upload; explicit attachment binds all profiles and preserves exact bytes', async () => {
  mount();
  await select();
  expect(get).toHaveBeenCalledWith(41, 1, expect.any(AbortSignal));
  expect(create).not.toHaveBeenCalled();
  expect(upload).not.toHaveBeenCalled();
  expect(screen.getAllByRole('checkbox')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await screen.findByText('Original bytes retained. Geometry and reported revision remain unapproved.');
  expect(create.mock.calls[0][2].targets).toEqual(fixture.request.targets);
  expect(create.mock.calls[0][2].expected_input_sha256).toBe(fixture.revision.content_sha256);
  expect(upload.mock.calls[0].slice(0, 4)).toEqual([41, 1, 51, 2]);
  expect(new Uint8Array(upload.mock.calls[0][4])).toEqual(new Uint8Array(await fixture.file.arrayBuffer()));
  expect(finalize).not.toHaveBeenCalled();
});

test('lost upload reply is finalized with the original intent without resending bytes', async () => {
  upload.mockRejectedValueOnce(new Error('Connection lost'));
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Check attachment' }));
  await screen.findByText('Original bytes retained. No resend was needed.');
  expect(create).toHaveBeenCalledTimes(1);
  expect(upload).toHaveBeenCalledTimes(1);
  expect(finalize).toHaveBeenCalledWith(41, 1, 51, 2, expect.any(AbortSignal));
});

test('a failed finalize never uploads automatically; resending requires another explicit action', async () => {
  upload.mockRejectedValueOnce(new Error('Connection lost'));
  finalize.mockRejectedValueOnce({ response: { status: 503, data: { detail: 'No verified attempt is readable.' } } });
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Check attachment' }));
  const resend = await screen.findByRole('button', { name: 'Resend original' });
  expect(upload).toHaveBeenCalledTimes(1);
  fireEvent.click(resend);
  await screen.findByText('Original bytes retained. Geometry and reported revision remain unapproved.');
  expect(upload).toHaveBeenCalledTimes(2);
  expect(create).toHaveBeenCalledTimes(1);
  expect(upload.mock.calls[1][2]).toBe(upload.mock.calls[0][2]);
});

test('lost intent reply retries the exact command UUID before completion recovery', async () => {
  create.mockRejectedValueOnce(new Error('Intent response lost'));
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Check attachment' }));
  await screen.findByText('Original bytes retained. Geometry and reported revision remain unapproved.');
  expect(create.mock.calls[1][2]).toEqual(create.mock.calls[0][2]);
  expect(finalize).toHaveBeenCalledTimes(1);
  expect(upload).not.toHaveBeenCalled();
});

test('refuses 101 files before reading any, and same name with different bytes remains unmatched', async () => {
  mount();
  const files = Array.from({ length: 101 }, () => originalFile('x'));
  fireEvent.change(await screen.findByLabelText(fileLabel), { target: { files } });
  await screen.findByText('Select up to 100 original DXFs. No files were read or uploaded.');
  expect(files.every(file => jest.mocked(file.arrayBuffer).mock.calls.length === 0)).toBe(true);
  fireEvent.change(screen.getByLabelText(fileLabel), {
    target: { files: [originalFile('different', fixture.file.name)] },
  });
  await screen.findByText(/No saved profile has this original-byte hash/);
  expect(create).not.toHaveBeenCalled();
});

test('only selected matching profiles are submitted, and a forged receipt is not marked retained', async () => {
  upload.mockImplementationOnce(async () => ({
    ...(await sourceIntent(create.mock.calls[0][2], fixture.parts, true)),
    company_id: 99,
  }));
  mount();
  await select();
  fireEvent.click(screen.getByRole('checkbox', { name: /Second profile/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await screen.findByText(/attachment does not match this saved revision/i);
  expect(create.mock.calls[0][2].targets).toEqual([fixture.request.targets[0]]);
  expect(
    screen.queryByText('Original bytes retained. Geometry and reported revision remain unapproved.')
  ).not.toBeInTheDocument();
});

test('read-only history can view receipts but cannot select files or resume another actor', async () => {
  const item = await sourceIntent(fixture.request, fixture.parts);
  list.mockResolvedValue(sourcePage([{ ...item, can_resume: false }], false));
  mount();
  await screen.findByText(/Only the original actor and credential/);
  expect(screen.queryByLabelText(fileLabel)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Check and finish attachment' })).not.toBeInTheDocument();
  expect(create).not.toHaveBeenCalled();
});

test('company change aborts in-flight bytes and ignores a late completed reply', async () => {
  let finish!: (value: Awaited<ReturnType<typeof sourceIntent>>) => void;
  upload.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const rendered = mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
  const signal = upload.mock.calls[0][5];
  mockCompanyId = 99;
  rendered.rerender(
    <CADSourceAttachments target={{ ...fixture.revision, company_id: 99 }} onBack={onBack} registerCloseGuard={guard} />
  );
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    finish(await sourceIntent(create.mock.calls[0][2], fixture.parts, true));
  });
  expect(
    screen.queryByText('Original bytes retained. Geometry and reported revision remain unapproved.')
  ).not.toBeInTheDocument();
});

test('leaving a local selection is guarded and does not create an upload', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Back to drafts' }));
  expect(confirm).toHaveBeenCalled();
  expect(onBack).not.toHaveBeenCalled();
  expect(create).not.toHaveBeenCalled();
  confirm.mockRestore();
});

test('429 pauses without discarding selections or silently retrying; explicit resume keeps the UUID', async () => {
  create.mockRejectedValueOnce({
    response: { status: 429, headers: { 'retry-after': '1' }, data: { detail: 'Rate limit' } },
  });
  mount();
  await select();
  fireEvent.click(screen.getByRole('button', { name: 'Attach selected originals' }));
  const resume = await screen.findByRole('button', { name: 'Resume paused batch' });
  expect(resume).toBeDisabled();
  expect(create).toHaveBeenCalledTimes(1);
  expect(upload).not.toHaveBeenCalled();
  await waitFor(() => expect(resume).toBeEnabled(), { timeout: 3000 });
  expect(create).toHaveBeenCalledTimes(1);
  fireEvent.click(resume);
  await screen.findByText('Original bytes retained. Geometry and reported revision remain unapproved.');
  expect(create.mock.calls[1][2]).toEqual(create.mock.calls[0][2]);
  expect(upload).toHaveBeenCalledTimes(1);
});
