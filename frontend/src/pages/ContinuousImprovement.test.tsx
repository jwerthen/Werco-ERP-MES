import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ContinuousImprovement from './ContinuousImprovement';
import continuousImprovement from '../services/continuousImprovement';
import type { ImprovementDetail, ImprovementMetadata } from '../types/continuousImprovement';

jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 7, company_id: 1 } }) }));
jest.mock('../services/continuousImprovement', () => ({
  __esModule: true,
  default: {
    metadata: jest.fn(),
    list: jest.fn(),
    detail: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    comment: jest.fn(),
  },
}));
const mocked = continuousImprovement as jest.Mocked<typeof continuousImprovement>;
const metadata: ImprovementMetadata = {
  categories: [
    {
      value: 'poka_yoke',
      label: 'Poka-yoke / Mistake proofing',
      description: 'Prevent or detect mistakes at the source.',
    },
    { value: 'five_s', label: '5S / Workplace organization' },
  ],
  priorities: [
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
  ],
  statuses: [
    { value: 'new', label: 'New' },
    { value: 'under_review', label: 'Under Review' },
    { value: 'approved', label: 'Approved' },
    { value: 'in_progress', label: 'In Progress' },
    { value: 'implemented', label: 'Implemented' },
    { value: 'on_hold', label: 'On Hold' },
    { value: 'declined', label: 'Declined' },
  ],
  owners: [{ id: 7, name: 'Shop Manager' }],
  can_manage: true,
};
const suggestion: ImprovementDetail = {
  id: 42,
  company_id: 1,
  title: 'Key the assembly fixture',
  problem: 'Parts can load backwards.',
  proposed_solution: 'Add a locating pin.',
  expected_benefit: 'Reduce orientation defects from 3 to 0 per batch.',
  category: 'poka_yoke',
  priority: 'medium',
  area: 'Assembly',
  owner_id: 7,
  owner_name: 'Shop Manager',
  status: 'new',
  target_date: '2026-10-05',
  implementation_notes: null,
  created_by: 7,
  created_by_name: 'Shop Manager',
  updated_by: 7,
  updated_by_name: 'Shop Manager',
  created_at: '2026-09-28T14:03:05Z',
  updated_at: '2026-09-28T14:03:05Z',
  reviewed_at: null,
  implemented_at: null,
  version: 1,
  history: [
    {
      id: 1,
      kind: 'submitted',
      actor_id: 7,
      actor_name: 'Shop Manager',
      created_at: '2026-09-28T14:03:05Z',
      body: null,
      changes: { status: { from: null, to: 'new' } },
    },
  ],
};
const counts = { new: 1, under_review: 2, approved: 0, in_progress: 3, implemented: 4, on_hold: 0, declined: 0 };

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  mocked.metadata.mockResolvedValue(metadata);
  mocked.list.mockResolvedValue({ items: [suggestion], total: 1, status_counts: counts });
  mocked.detail.mockResolvedValue(suggestion);
  mocked.create.mockResolvedValue(suggestion);
  mocked.update.mockResolvedValue({
    ...suggestion,
    version: 2,
    status: 'under_review',
    reviewed_at: '2026-09-28T15:00:00Z',
  });
  mocked.comment.mockResolvedValue({ ...suggestion, version: 2 });
});

async function openSuggestion() {
  const links = await screen.findAllByRole('button', { name: suggestion.title });
  fireEvent.click(links[0]);
  const dialog = await screen.findByRole('dialog', { name: 'Suggestion details' });
  await within(dialog).findByRole('heading', { name: suggestion.title });
  return dialog;
}

async function editSuggestion() {
  const dialog = await openSuggestion();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Edit suggestion' }));
  await within(dialog).findByRole('heading', { name: 'Edit suggestion' });
  return dialog;
}

it.each(['viewer', 'company read-only'])(
  'keeps %s sessions read-only while exposing timestamped history',
  async mode => {
    if (mode === 'viewer') mocked.metadata.mockResolvedValue({ ...metadata, can_manage: false });
    else sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '7', cid: 1, ro: true }))}.signature`);
    render(<ContinuousImprovement />);
    const dialog = await openSuggestion();
    expect(screen.queryByRole('button', { name: 'New suggestion' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Edit suggestion' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Post comment' })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('heading', { name: 'Activity history' })).toBeInTheDocument();
    expect(within(dialog).getAllByText(/Sep 28, 2026, 9:03:05 AM CDT/)).toHaveLength(3);
    expect(mocked.update).not.toHaveBeenCalled();
  }
);

it('submits a categorized suggestion with ownership and protects against double submission', async () => {
  let resolveSave!: (value: ImprovementDetail) => void;
  mocked.create.mockImplementation(
    () =>
      new Promise(resolve => {
        resolveSave = resolve;
      })
  );
  render(<ContinuousImprovement />);
  fireEvent.click(await screen.findByRole('button', { name: 'New suggestion' }));
  const dialog = screen.getByRole('dialog', { name: 'New suggestion' });
  fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: '  Key the assembly fixture  ' } });
  fireEvent.change(within(dialog).getByLabelText(/^Category/), { target: { value: 'poka_yoke' } });
  fireEvent.change(within(dialog).getByLabelText(/^Problem/), { target: { value: 'Parts can load backwards.' } });
  fireEvent.change(within(dialog).getByLabelText(/^Proposed solution/), { target: { value: 'Add a locating pin.' } });
  fireEvent.change(within(dialog).getByLabelText(/^Expected benefit/), {
    target: { value: 'Reduce orientation defects from 3 to 0 per batch.' },
  });
  fireEvent.change(within(dialog).getByLabelText(/^Owner/), { target: { value: '7' } });
  fireEvent.change(within(dialog).getByLabelText(/^Target date/), { target: { value: '2026-10-05' } });
  const form = within(dialog).getByRole('button', { name: 'Submit suggestion' }).closest('form')!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(mocked.create).toHaveBeenCalledTimes(1);
  expect(mocked.create).toHaveBeenCalledWith(
    expect.objectContaining({ title: suggestion.title, category: 'poka_yoke', owner_id: 7, target_date: '2026-10-05' })
  );
  expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
  resolveSave(suggestion);
  await screen.findByRole('heading', { name: suggestion.title });
});

it('requires status decision notes and implementation results before completing an improvement', async () => {
  render(<ContinuousImprovement />);
  const dialog = await editSuggestion();
  fireEvent.change(within(dialog).getByLabelText('Status'), { target: { value: 'implemented' } });
  const form = within(dialog).getByRole('button', { name: 'Save changes' }).closest('form')!;
  fireEvent.submit(form);
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Add a change note');
  expect(mocked.update).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByLabelText(/^Change note/), {
    target: { value: 'Trial accepted after two shifts.' },
  });
  fireEvent.submit(form);
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Record implementation results');
  expect(mocked.update).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByLabelText(/^Implementation results/), {
    target: { value: 'Locating pin installed. Zero defects in 200 parts.' },
  });
  fireEvent.submit(form);
  await waitFor(() =>
    expect(mocked.update).toHaveBeenCalledWith(
      42,
      expect.objectContaining({
        expected_version: 1,
        status: 'implemented',
        change_note: 'Trial accepted after two shifts.',
        implementation_notes: 'Locating pin installed. Zero defects in 200 parts.',
      })
    )
  );
});

it('preserves a draft on conflict and requires reviewing the latest version before retrying', async () => {
  mocked.update.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'Stale version' } } });
  render(<ContinuousImprovement />);
  const dialog = await editSuggestion();
  fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: 'My improved fixture plan' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Your draft is preserved');
  expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('My improved fixture plan');
  expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled();
  mocked.detail.mockResolvedValue({
    ...suggestion,
    title: 'Another manager changed this',
    area: 'Welding',
    status: 'under_review',
    version: 3,
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Review latest version' }));
  expect(await within(dialog).findByText('Another manager changed this')).toBeInTheDocument();
  expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('My improved fixture plan');
  fireEvent.click(within(dialog).getByRole('button', { name: 'I reviewed the latest version; keep my draft' }));
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(mocked.update).toHaveBeenLastCalledWith(
      42,
      expect.objectContaining({
        expected_version: 3,
        title: 'My improved fixture plan',
        area: 'Welding',
        status: 'under_review',
      })
    )
  );
});

it('protects unsaved entries when closing a suggestion form', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  try {
    render(<ContinuousImprovement />);
    fireEvent.click(await screen.findByRole('button', { name: 'New suggestion' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: 'Do not lose this idea' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close suggestion' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByLabelText(/^Title/)).toHaveValue('Do not lose this idea');
  } finally {
    confirm.mockRestore();
  }
});

it('posts comments against the version being reviewed and keeps text on an error', async () => {
  mocked.comment.mockRejectedValueOnce({ response: { status: 503, data: { detail: 'Unable to save audit record.' } } });
  render(<ContinuousImprovement />);
  const dialog = await openSuggestion();
  fireEvent.change(within(dialog).getByLabelText('Add a comment'), { target: { value: 'Test this on second shift.' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Post comment' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Unable to save audit record.');
  expect(mocked.comment).toHaveBeenCalledWith(42, 1, 'Test this on second shift.');
  expect(within(dialog).getByLabelText('Add a comment')).toHaveValue('Test this on second shift.');
});

it('uses server filters without changing global summary counts', async () => {
  render(<ContinuousImprovement />);
  const tile = await screen.findByRole('button', { name: /Under review/ });
  await waitFor(() => expect(within(tile).getByText('2')).toBeInTheDocument());
  fireEvent.click(tile);
  await waitFor(() =>
    expect(mocked.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: 'under_review', skip: 0, limit: 25 })
    )
  );
  fireEvent.change(screen.getByLabelText('Filter by category'), { target: { value: 'five_s' } });
  await waitFor(() =>
    expect(mocked.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: 'under_review', category: 'five_s' })
    )
  );
  expect(within(screen.getByRole('button', { name: /Implemented/ })).getByText('4')).toBeInTheDocument();
});

it('retries a failed initial load without presenting an empty register as success', async () => {
  mocked.list.mockRejectedValueOnce(new Error('offline'));
  render(<ContinuousImprovement />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load suggestions');
  expect(screen.queryByText('Start with one improvement')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findAllByRole('button', { name: suggestion.title })).toHaveLength(2);
});

it('does not display a previous company detail response after switching company sessions', async () => {
  const token = (companyId: number) =>
    `header.${btoa(JSON.stringify({ sub: '7', cid: companyId, ro: false }))}.signature`;
  sessionStorage.setItem('token', token(1));
  let resolveDetail!: (value: ImprovementDetail) => void;
  mocked.detail.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        resolveDetail = resolve;
      })
  );
  render(<ContinuousImprovement />);
  fireEvent.click((await screen.findAllByRole('button', { name: suggestion.title }))[0]);
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  mocked.list.mockResolvedValue({
    items: [{ ...suggestion, id: 50, company_id: 2, title: 'New company improvement' }],
    total: 1,
    status_counts: counts,
  });
  sessionStorage.setItem('token', token(2));
  fireEvent(window, new Event('werco:auth-token-changed'));
  expect(await screen.findAllByRole('button', { name: 'New company improvement' })).toHaveLength(2);
  resolveDetail(suggestion);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.queryByText(suggestion.title)).not.toBeInTheDocument();
});

it('hides advanced filters on first-run while retaining a way to clear an empty filtered result', async () => {
  const emptyCounts = { new: 0, under_review: 0, approved: 0, in_progress: 0, implemented: 0, on_hold: 0, declined: 0 };
  mocked.list.mockResolvedValue({ items: [], total: 0, status_counts: emptyCounts });
  render(<ContinuousImprovement />);
  await screen.findByText('Start with one improvement');
  expect(screen.queryByLabelText('Filter by category')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Search suggestions')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /Under review/ }));
  expect(await screen.findByLabelText('Filter by status')).toHaveValue('under_review');
  await screen.findByText('No matching suggestions');
  fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
  await screen.findByText('Start with one improvement');
  expect(screen.queryByLabelText('Filter by category')).not.toBeInTheDocument();
});
