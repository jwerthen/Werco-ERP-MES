import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import api from '../../services/api';
import { createBlankProject, projectToFile } from './lib/quote-project';
import { createBlankQuote } from './lib/quoting';
import { rect } from './lib/nesting';
import type { NestingDraftPage, NestingDraftRevision } from '../../types/nestingDraft';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingMaterials: jest.fn(),
    listNestingDrafts: jest.fn(),
    listNestingDraftRevisions: jest.fn(),
    getNestingDraftRevision: jest.fn(),
    saveNestingDraft: jest.fn(),
  },
}));
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: jest.fn() }) }));
const list = jest.mocked(api.listNestingDrafts),
  get = jest.mocked(api.getNestingDraftRevision),
  save = jest.mocked(api.saveNestingDraft);
const initial = () => ({
  ...createBlankQuote(),
  name: 'Current estimate',
  parts: [{ id: 'plate', name: 'Synthetic plate', quantity: 1, rotate: false, color: 0, loops: [rect(25.4, 25.4)] }],
});
function receipt(): NestingDraftRevision {
  const project = createBlankProject(initial());
  project.name = 'Saved team estimate';
  return {
    schema_version: 1,
    draft_id: 41,
    company_id: 2,
    revision_number: 1,
    draft_version: 1,
    name: project.name,
    status: 'DRAFT',
    content_sha256: 'a'.repeat(64),
    payload_schema_version: 4,
    payload_bytes: 2048,
    created_by: 7,
    created_at: '2026-09-08T18:00:00Z',
    review_issues: [],
    estimate: projectToFile(project),
  };
}
const page = (): NestingDraftPage => ({ schema_version: 1, items: [receipt()], total: 1, page: 1, per_page: 10 });
async function mount() {
  let rendered!: ReturnType<typeof render>;
  await act(async () => {
    rendered = render(
      <NestingPortalContext.Provider value={document.body}>
        <NestingWorkspace initialQuote={initial()} companyId={2} estimatorId={7} canSaveDrafts />
      </NestingPortalContext.Provider>
    );
  });
  return rendered;
}
async function openDialog() {
  fireEvent.click(screen.getByRole('button', { name: 'Team drafts' }));
  await screen.findByRole('button', { name: 'Open Saved team estimate revision 1' });
}
function dirty() {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}
beforeEach(() => {
  jest.clearAllMocks();
  jest
    .mocked(api.getNestingMaterials)
    .mockResolvedValue({ schema_version: 1, items: [], total: 0, offset: 0, limit: 200 });
  list.mockResolvedValue(page());
  get.mockResolvedValue(receipt());
  save.mockResolvedValue(receipt());
});

test.each(['New', 'local file'] as const)(
  '%s starts an independent document instead of appending to the linked team draft',
  async action => {
    const { container } = await mount();
    await openDialog();
    fireEvent.click(screen.getByRole('button', { name: 'Open Saved team estimate revision 1' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Team drafts' })).toHaveAttribute('title', 'Team draft #41, revision 1')
    );
    if (action === 'New') {
      fireEvent.click(screen.getByRole('button', { name: 'New' }));
      expect(screen.getByText('0 designs')).toBeInTheDocument();
    } else {
      const local = createBlankProject();
      local.name = 'Independent local file';
      const text = JSON.stringify(projectToFile(local));
      const file = new File([text], 'independent.estimate.json', { type: 'application/json' });
      Object.defineProperty(file, 'text', { value: async () => text });
      const input = container.querySelector<HTMLInputElement>('input[type=file][accept=".json"]');
      if (!input) throw new Error('Missing local estimate input');
      await act(async () => fireEvent.change(input, { target: { files: [file] } }));
    }
    expect(screen.getByRole('button', { name: 'Team drafts' })).toHaveAttribute('title', 'Save or open a team draft');
    await openDialog();
    fireEvent.click(screen.getByRole('button', { name: 'Save team draft' }));
    await screen.findByText(/Saved draft #41/);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0][0].target).toBeUndefined();
    expect(JSON.parse(save.mock.calls[0][0].estimateJson).name).toBe(
      action === 'New' ? 'New material estimate' : 'Independent local file'
    );
  }
);

test('double Save is single-flight and a late successful save cannot mark newer edits clean', async () => {
  let finish!: (value: NestingDraftRevision) => void;
  save.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  await mount();
  fireEvent.change(screen.getByLabelText('Estimate name'), { target: { value: 'Submitted inputs' } });
  await openDialog();
  const button = screen.getByRole('button', { name: 'Save team draft' });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(button).toBeDisabled();
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText('Estimate name'), { target: { value: 'Newer unsaved inputs' } });
  await act(async () => finish({ ...receipt(), estimate: JSON.parse(save.mock.calls[0][0].estimateJson) }));
  expect(screen.getByLabelText('Estimate name')).toHaveValue('Newer unsaved inputs');
  expect(dirty()).toBe(true);
  expect(JSON.parse(save.mock.calls[0][0].estimateJson).name).toBe('Submitted inputs');
});

test('invalid saved geometry cannot overwrite the current workspace or establish a team link', async () => {
  const invalid = createBlankProject(initial());
  invalid.groups[0].quote.parts[0].loops = [{ type: 'circle', cx: 0, cy: 0, r: 0 }];
  // Construct malformed server JSON without calling the client writer, which
  // correctly refuses to serialize this geometry itself.
  const malformed = {
    ...receipt(),
    estimate: {
      ...receipt().estimate,
      groups: [
        {
          id: 'group-1',
          quote: {
            ...projectToFile(createBlankProject(initial())).groups[0].quote,
            parts: [{ ...invalid.groups[0].quote.parts[0], loops: [{ type: 'circle', cx: 0, cy: 0, r: 0 }] }],
          },
        },
      ],
    },
  };
  get.mockResolvedValue(malformed);
  await mount();
  await openDialog();
  fireEvent.click(screen.getByRole('button', { name: 'Open Saved team estimate revision 1' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Estimate name')).toHaveValue('Current estimate');
  expect(screen.getByLabelText('Quantity for Synthetic plate')).toHaveValue(1);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Team nesting drafts' })).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: 'Team drafts' })).toHaveAttribute('title', 'Save or open a team draft');
  expect(save).not.toHaveBeenCalled();
});
