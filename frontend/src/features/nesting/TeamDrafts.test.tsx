import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import TeamDrafts from './TeamDrafts';
import { NestingPortalContext } from './PortalContext';
import api from '../../services/api';
import { createBlankProject, projectToFile, type QuoteProject } from './lib/quote-project';
import { catalogQuoteFixture } from '../../test-utils/nestingCatalogFixtures';
import type { NestingDraftPage, NestingDraftRevision } from '../../types/nestingDraft';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listNestingDrafts: jest.fn(),
    listNestingDraftRevisions: jest.fn(),
    getNestingDraftRevision: jest.fn(),
    saveNestingDraft: jest.fn(),
  },
}));
const list = jest.mocked(api.listNestingDrafts);
const history = jest.mocked(api.listNestingDraftRevisions);
const get = jest.mocked(api.getNestingDraftRevision);
const save = jest.mocked(api.saveNestingDraft);
const onOpen = jest.fn<void, [QuoteProject]>();
const onSaved = jest.fn();
const project = createBlankProject();
function receipt(revision = 1, estimate = projectToFile(project)): NestingDraftRevision {
  return {
    schema_version: 1,
    draft_id: 41,
    company_id: 2,
    revision_number: revision,
    draft_version: revision,
    name: 'Bracket estimate',
    status: 'DRAFT',
    content_sha256: 'a'.repeat(64),
    payload_schema_version: estimate.version,
    payload_bytes: 2048,
    created_by: 7,
    created_at: '2026-09-08T18:00:00Z',
    review_issues: [],
    estimate,
  };
}
function page(items: NestingDraftRevision[] = []): NestingDraftPage {
  return { schema_version: 1, items, total: items.length, page: 1, per_page: 10 };
}
function mount(overrides: Partial<React.ComponentProps<typeof TeamDrafts>> = {}) {
  return render(
    <TeamDrafts
      companyId={2}
      project={project}
      canSave
      disabled={false}
      dirty={false}
      onOpen={onOpen}
      onSaved={onSaved}
      {...overrides}
    />,
    { wrapper: ({ children }) => <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider> }
  );
}
async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Team drafts' }));
  await screen.findByRole('dialog', { name: 'Team nesting drafts' });
  await waitFor(() => expect(screen.queryByText('Loading drafts…')).not.toBeInTheDocument());
}

beforeEach(() => {
  jest.clearAllMocks();
  list.mockResolvedValue(page());
  history.mockResolvedValue(page([receipt()]));
  get.mockResolvedValue(receipt());
  save.mockResolvedValue(receipt());
});

test('does not fetch or restore a draft on entry; save is explicit and uses imperial input data', async () => {
  mount();
  expect(list).not.toHaveBeenCalled();
  expect(get).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Save team draft' }));
  await screen.findByText(/Saved draft #41, revision 1/);
  expect(save).toHaveBeenCalledTimes(1);
  const request = save.mock.calls[0][0];
  expect(request.companyId).toBe(2);
  expect(request.requestKey).toMatch(/^[a-f0-9-]{36}$/);
  expect(request.target).toBeUndefined();
  expect(JSON.parse(request.estimateJson)).toEqual(projectToFile(project));
  expect(onSaved).toHaveBeenCalledWith(JSON.stringify(project));
  expect(onOpen).not.toHaveBeenCalled();
});

test('read-only users can open but cannot save', async () => {
  list.mockResolvedValue(page([receipt()]));
  mount({ canSave: false });
  await open();
  expect(screen.queryByRole('button', { name: 'Save team draft' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
  expect(save).not.toHaveBeenCalled();
});

test('retry after uncertain failure reuses the exact request even if current inputs change', async () => {
  save.mockRejectedValueOnce(new Error('Network interrupted'));
  const rendered = mount();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Save team draft' }));
  await screen.findByRole('alert');
  const changed = { ...project, name: 'Edited after timeout' };
  rendered.rerender(
    <TeamDrafts companyId={2} project={changed} canSave disabled={false} dirty onOpen={onOpen} onSaved={onSaved} />
  );
  fireEvent.click(screen.getByRole('button', { name: 'Retry previous save' }));
  await screen.findByText(/Saved draft #41/);
  expect(save.mock.calls[1][0]).toEqual(save.mock.calls[0][0]);
  expect(onSaved).toHaveBeenCalledWith(JSON.stringify(project));
});

test('opening history retains that revision version; a conflict can be saved explicitly as a new draft', async () => {
  list.mockResolvedValue(page([receipt(3)]));
  mount();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'History for Bracket estimate' }));
  await screen.findByRole('button', { name: 'Open Bracket estimate revision 1' });
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  await waitFor(() => expect(onOpen).toHaveBeenCalled());
  await open();
  save.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'A newer revision exists.' } } });
  fireEvent.click(screen.getByRole('button', { name: 'Save next revision' }));
  await screen.findByText('A newer revision exists.');
  expect(save.mock.calls[0][0].target).toEqual({ draftId: 41, expectedVersion: 1 });
  expect(screen.queryByRole('button', { name: 'Retry previous save' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Save as new draft' }));
  await screen.findByText(/Saved draft #41/);
  expect(save.mock.calls[1][0].target).toBeUndefined();
  expect(save.mock.calls[1][0].requestKey).not.toBe(save.mock.calls[0][0].requestKey);
});

test('opening a team revision clears applied catalog prices and requires confirmation before replacing unsaved inputs', async () => {
  const bound = createBlankProject(catalogQuoteFixture().quote);
  list.mockResolvedValue(page([receipt(1, projectToFile(bound))]));
  get.mockResolvedValue(receipt(1, projectToFile(bound)));
  mount({ dirty: true });
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  expect(get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Keep current estimate' }));
  expect(onOpen).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Replace current estimate' }));
  await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
  const quote = onOpen.mock.calls[0][0].groups[0].quote;
  expect(quote.options.every(option => option.price === null)).toBe(true);
  expect(quote.materialBinding?.acknowledgement).toBeUndefined();
  expect(quote.materialBinding?.catalog.id).toBe(11);
});

test('rejects foreign-company receipts and leaves the current estimate untouched', async () => {
  list.mockResolvedValue(page([receipt()]));
  get.mockResolvedValue({ ...receipt(), company_id: 99 });
  mount();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/another company/);
  expect(onOpen).not.toHaveBeenCalled();
});

test('late reads after leaving the section cannot reopen a draft', async () => {
  list.mockResolvedValue(page([receipt()]));
  let finish!: (value: NestingDraftRevision) => void;
  get.mockImplementation(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const rendered = mount();
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Open Bracket estimate revision 1' }));
  rendered.unmount();
  await act(async () => finish(receipt()));
  expect(onOpen).not.toHaveBeenCalled();
  expect(get.mock.calls[0][2]?.aborted).toBe(true);
});
