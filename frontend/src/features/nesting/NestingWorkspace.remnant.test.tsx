import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { remnantStageInput } from '../../test-utils/remnantStageFixtures';
import { remnantPlanningFixture } from '../../test-utils/remnantPlanningFixtures';
import { buildRemnantPlan } from './lib/remnant-evidence';
import { calculateRemnantProject } from './lib/remnant-planning';
import { compareSheets, quoteFromFile, type Quote } from './lib/quoting';
import * as client from './lib/nesting-worker-client';
import type RemnantPiecePicker from './RemnantPiecePicker';
const mockToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockToast }) }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingMaterials: jest.fn().mockResolvedValue({ schema_version: 1, items: [], total: 0, offset: 0, limit: 200 }),
  },
}));
jest.mock('./RemnantPiecePicker', () => ({
  __esModule: true,
  default: (props: React.ComponentProps<typeof RemnantPiecePicker>) => (
    <button
      onClick={() =>
        void (async () => {
          const fixture = await remnantPlanningFixture();
          props.onSelect(
            await buildRemnantPlan({
              resolution: fixture.resolution,
              companyId: props.companyId,
              groupId: props.groupId,
              quote: props.quote,
              family: 'Carbon steel',
              requiredGrade: 'A36',
              reason: 'Explicit fixture reassignment',
              zoneClearanceIn: '0.125',
            })
          );
        })()
      }
    >
      Confirm test piece
    </button>
  ),
}));
afterEach(() => {
  jest.restoreAllMocks();
  mockToast.mockClear();
});
function mount(initialQuote?: Quote) {
  return render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace companyId={2} estimatorId={7} canPlanRemnants initialQuote={initialQuote} />
    </NestingPortalContext.Provider>
  );
}
async function openFile(container: HTMLElement, raw: unknown) {
  const file = new File([JSON.stringify(raw)], 'planning.json');
  Object.defineProperty(file, 'text', { value: async () => JSON.stringify(raw) });
  const input = container.querySelector('input[type="file"][accept=".json"]');
  if (!input) throw new Error('Missing file input');
  fireEvent.change(input, { target: { files: [file] } });
  await screen.findByText('Refresh and reaffirm assignment');
}
test('Open preserves selection but waits for explicit reassignment; edits invalidate it and New stays empty', async () => {
  const ordinary = jest.spyOn(client, 'compareSheetsInWorker').mockImplementation(async quote => compareSheets(quote));
  const staged = jest.spyOn(client, 'compareProjectStagesInWorker').mockImplementation(async (raw, digest, options) => {
    for await (const frame of calculateRemnantProject(raw, digest)) {
      if (frame.type === 'stage') options.onStage(frame);
      else return frame;
    }
    throw new Error('Missing summary');
  });
  let view!: ReturnType<typeof mount>;
  await act(async () => {
    view = mount();
  });
  expect(screen.getByText('0 designs')).toBeInTheDocument();
  expect(staged).not.toHaveBeenCalled();
  await openFile(view.container, await remnantStageInput());
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await waitFor(() => expect(ordinary).toHaveBeenCalledTimes(1));
  expect(staged).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh and reaffirm assignment' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Refresh and reaffirm assignment' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm test piece' }));
  await screen.findByText(/Source checked and assignment confirmed/);
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByText(/Planned search finished/);
  expect(staged).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Edit rotation and grain for Synthetic plate' }));
  fireEvent.change(screen.getByLabelText('Allowed rotation for Synthetic plate'), { target: { value: 'half-turn' } });
  expect(screen.getByText(/Refresh the source and reaffirm/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Remove conditional piece' }));
  expect(screen.queryByRole('button', { name: 'Refresh and reaffirm assignment' })).not.toBeInTheDocument();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'New' })));
  expect(screen.getByText('0 designs')).toBeInTheDocument();
  expect(staged).toHaveBeenCalledTimes(1);
});

test('a timeout midway through baseline options retains the earlier best valid layout and partial export', async () => {
  const raw = await remnantStageInput();
  const quote = quoteFromFile(raw.groups[0].quote);
  quote.options.push({ ...quote.options[0], id: 'larger', width: 254 });
  jest.spyOn(client, 'compareProjectStagesInWorker').mockImplementation(async (input, digest, options) => {
    const iterator = calculateRemnantProject(input, digest);
    const first = await iterator.next();
    if (first.value?.type !== 'stage') throw new Error('Missing first baseline');
    options.onStage(first.value);
    await iterator.return(undefined);
    throw new Error('The staged nest reached its 120-second limit. Earlier stages remain available.');
  });
  let view!: ReturnType<typeof mount>;
  await act(async () => {
    view = mount(quote);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Choose recorded piece' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm test piece' }));
  await screen.findByText(/Source checked and assignment confirmed/);
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByText(/The staged nest reached its 120-second limit/);
  expect(screen.getByRole('img', { name: /^Layout on / })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export review record' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: 'View recorded piece' })).not.toBeInTheDocument();
  view.unmount();
});
