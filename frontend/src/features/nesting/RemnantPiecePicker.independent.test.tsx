import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import RemnantPiecePicker from './RemnantPiecePicker';
import api from '../../services/api';
import { remnantPlanningFixture } from '../../test-utils/remnantPlanningFixtures';
import type { RemnantResolution } from '../../types/remnantPlanning';

jest.mock('../../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 7 } }) }));
jest.mock('../../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 2 } }) }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getStockPieces: jest.fn(), resolveRemnantPlanningSnapshot: jest.fn() },
}));

const list = jest.mocked(api.getStockPieces);
const resolve = jest.mocked(api.resolveRemnantPlanningSnapshot);
let fixture: Awaited<ReturnType<typeof remnantPlanningFixture>>;
const onSelect = jest.fn();
const props = () => ({ companyId: 2, groupId: fixture.groupId, quote: fixture.quote, onSelect, onCancel: jest.fn() });

beforeEach(async () => {
  jest.resetAllMocks();
  fixture = await remnantPlanningFixture();
  list.mockResolvedValue(fixture.page);
  resolve.mockResolvedValue(fixture.resolution);
});

async function startFinalRefresh() {
  fireEvent.click(await screen.findByRole('button', { name: 'Review Café L-piece' }));
  await screen.findByRole('heading', { name: 'Café L-piece · observation 2' });
  fireEvent.change(screen.getByLabelText('Assign material family'), { target: { value: 'Carbon steel' } });
  fireEvent.change(screen.getByLabelText('Required grade for this entire group'), { target: { value: 'A36' } });
  fireEvent.change(screen.getByLabelText('Assignment reason'), {
    target: { value: 'Measured and identified by planner' },
  });
  let finish!: (value: RemnantResolution) => void;
  resolve.mockImplementationOnce(() => new Promise(value => (finish = value)));
  fireEvent.click(screen.getByRole('button', { name: 'Select this piece for the group' }));
  await waitFor(() => expect(resolve).toHaveBeenCalledTimes(2));
  return { finish, signal: resolve.mock.calls[1][3] };
}

test('a changed raw group cannot receive a selection whose final source refresh finishes late', async () => {
  const view = render(<RemnantPiecePicker {...props()} />);
  const pending = await startFinalRefresh();
  view.rerender(<RemnantPiecePicker {...props()} quote={{ ...fixture.quote, name: 'Changed material plan' }} />);
  expect(pending.signal?.aborted).toBe(true);
  await act(async () => pending.finish(fixture.resolution));
  expect(onSelect).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Select this piece for the group' })).not.toBeInTheDocument();

  // A fresh inspection remains possible; cancelling the obsolete command must not leave a stuck form.
  fireEvent.click(screen.getByRole('button', { name: 'Review Café L-piece' }));
  await screen.findByRole('heading', { name: 'Café L-piece · observation 2' });
  expect(screen.getByLabelText('Required grade for this entire group')).toHaveValue('');
});

test('unmount aborts the final refresh and refuses its otherwise valid late selection', async () => {
  const view = render(<RemnantPiecePicker {...props()} />);
  const pending = await startFinalRefresh();
  view.unmount();
  expect(pending.signal?.aborted).toBe(true);
  await act(async () => pending.finish(fixture.resolution));
  expect(onSelect).not.toHaveBeenCalled();
  expect(resolve).toHaveBeenCalledTimes(2);
});

test.each(['foreign company', 'duplicate piece'] as const)(
  'refuses an inconsistent %s page before source resolution',
  async kind => {
    list.mockResolvedValueOnce(
      kind === 'foreign company'
        ? { ...fixture.page, company_id: 3 }
        : { ...fixture.page, total: 2, items: [fixture.summary, { ...fixture.summary }] }
    );
    render(<RemnantPiecePicker {...props()} />);
    expect(await screen.findByRole('alert')).not.toBeEmptyDOMElement();
    expect(screen.queryByRole('button', { name: 'Review Café L-piece' })).not.toBeInTheDocument();
    expect(resolve).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
  }
);
