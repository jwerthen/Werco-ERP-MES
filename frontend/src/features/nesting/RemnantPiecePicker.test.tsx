import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import RemnantPiecePicker from './RemnantPiecePicker';
import api from '../../services/api';
import { remnantPlanningFixture } from '../../test-utils/remnantPlanningFixtures';
import type { RemnantResolution } from '../../types/remnantPlanning';

let mockCompanyId = 2;
let mockUserId = 7;
jest.mock('../../context/AuthContext', () => ({ useAuth: () => ({ user: { id: mockUserId } }) }));
jest.mock('../../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: mockCompanyId } }) }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getStockPieces: jest.fn(), resolveRemnantPlanningSnapshot: jest.fn() },
}));
const list = jest.mocked(api.getStockPieces),
  resolve = jest.mocked(api.resolveRemnantPlanningSnapshot);
let fixture: Awaited<ReturnType<typeof remnantPlanningFixture>>;
const onSelect = jest.fn(),
  onCancel = jest.fn();
const props = () => ({ companyId: 2, groupId: fixture.groupId, quote: fixture.quote, onSelect, onCancel });
beforeEach(async () => {
  jest.clearAllMocks();
  mockCompanyId = 2;
  mockUserId = 7;
  fixture = await remnantPlanningFixture();
  list.mockResolvedValue(fixture.page);
  resolve.mockResolvedValue(fixture.resolution);
});
async function inspect() {
  fireEvent.click(await screen.findByRole('button', { name: 'Review Café L-piece' }));
  await screen.findByRole('heading', { name: 'Café L-piece · observation 2' });
}
function fill() {
  fireEvent.change(screen.getByLabelText('Assign material family'), { target: { value: 'Carbon steel' } });
  fireEvent.change(screen.getByLabelText('Required grade for this entire group'), { target: { value: 'A36' } });
  fireEvent.change(screen.getByLabelText('Assignment reason'), {
    target: { value: 'Checked the required grade and recorded measurement' },
  });
}
test('explicit selection preserves true reported shape and refreshes before callback without inventory writes', async () => {
  render(<RemnantPiecePicker {...props()} />);
  expect(resolve).not.toHaveBeenCalled();
  await inspect();
  const svg = screen.getByRole('img', { name: 'Reported piece outline and unavailable zones in inches' });
  expect(svg.querySelector('path')?.getAttribute('d')).toContain('-0.000000001');
  expect(svg.querySelector('path')?.getAttribute('d')?.match(/M/g)).toHaveLength(2);
  expect(screen.getByLabelText('Assign material family')).toHaveValue('');
  expect(screen.getByLabelText('Additional unavailable-zone clearance (in)')).toHaveValue('0.375');
  fill();
  fireEvent.change(screen.getByLabelText('Additional unavailable-zone clearance (in)'), { target: { value: '1 1/8' } });
  fireEvent.click(screen.getByRole('button', { name: 'Select this piece for the group' }));
  await waitFor(() => expect(onSelect).toHaveBeenCalledTimes(1));
  expect(resolve).toHaveBeenCalledTimes(2);
  expect(resolve).toHaveBeenLastCalledWith(
    41,
    2,
    {
      expected_company_id: 2,
      expected_payload_sha256: fixture.snapshot.payloadSha256,
      expected_source_sha256: fixture.snapshot.sourceSha256,
    },
    expect.any(AbortSignal)
  );
  expect(onSelect.mock.calls[0][0]).toMatchObject({
    capacity: 1,
    zoneClearanceIn: '1.125',
    planningOnly: true,
    availabilityVerified: false,
  });
  expect(onSelect.mock.calls[0][0].snapshot.evidence).toEqual(fixture.snapshot.evidence);
});
test('grade mismatch and source drift block selection; form values remain reviewable', async () => {
  render(<RemnantPiecePicker {...props()} />);
  await inspect();
  fill();
  fireEvent.change(screen.getByLabelText('Required grade for this entire group'), { target: { value: 'a36' } });
  fireEvent.click(screen.getByRole('button', { name: 'Select this piece for the group' }));
  await screen.findByText(/does not match this group/);
  expect(onSelect).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Required grade for this entire group'), { target: { value: 'A36' } });
  resolve.mockRejectedValueOnce({
    response: { status: 409, data: { detail: 'Source changed; record a new observation.' } },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Select this piece for the group' }));
  await screen.findByText('Source changed; record a new observation.');
  expect(screen.getByLabelText('Required grade for this entire group')).toHaveValue('A36');
  expect(onSelect).not.toHaveBeenCalled();
});
test('withdrawn and stale rows cannot be inspected; inventory read does not require can_record', async () => {
  list.mockResolvedValue({
    ...fixture.page,
    items: [
      { ...fixture.summary, state: 'WITHDRAWN' },
      { ...fixture.summary, piece_id: 42, label: 'Stale source', source_status: 'changed' },
    ],
    total: 2,
  });
  render(<RemnantPiecePicker {...props()} />);
  expect(await screen.findByRole('button', { name: 'Review Café L-piece' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Review Stale source' })).toBeDisabled();
  expect(resolve).not.toHaveBeenCalled();
});
test('single-flight and company/actor changes discard late resolver responses', async () => {
  let release!: (value: RemnantResolution) => void;
  resolve.mockImplementation(
    () =>
      new Promise(value => {
        release = value;
      })
  );
  const view = render(<RemnantPiecePicker {...props()} />);
  const review = await screen.findByRole('button', { name: 'Review Café L-piece' });
  fireEvent.click(review);
  fireEvent.click(review);
  expect(resolve).toHaveBeenCalledTimes(1);
  mockUserId = 8;
  view.rerender(<RemnantPiecePicker {...props()} />);
  await act(async () => release(fixture.resolution));
  expect(screen.queryByRole('heading', { name: 'Café L-piece · observation 2' })).not.toBeInTheDocument();
  mockCompanyId = 3;
  view.rerender(<RemnantPiecePicker {...props()} />);
  expect(screen.getByRole('alert')).toHaveTextContent('Select this nest’s company');
  expect(onSelect).not.toHaveBeenCalled();
});
test('changed group input invalidates preview and assignment; identical objects do not reset the form', async () => {
  const view = render(<RemnantPiecePicker {...props()} />);
  await inspect();
  fill();
  view.rerender(<RemnantPiecePicker {...props()} quote={{ ...fixture.quote }} />);
  expect(screen.getByLabelText('Assignment reason')).toHaveValue('Checked the required grade and recorded measurement');
  view.rerender(<RemnantPiecePicker {...props()} quote={{ ...fixture.quote, margin: 0.5 }} />);
  expect(screen.queryByRole('button', { name: 'Select this piece for the group' })).not.toBeInTheDocument();
  expect(onSelect).not.toHaveBeenCalled();
});
test('untrusted resolver identity and read-only POST refusal remain actionable without selection', async () => {
  resolve.mockResolvedValueOnce({ ...fixture.resolution, company_id: 3 });
  render(<RemnantPiecePicker {...props()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review Café L-piece' }));
  await screen.findByText(/differs from the selected piece/);
  expect(screen.queryByRole('button', { name: 'Select this piece for the group' })).not.toBeInTheDocument();
  resolve.mockRejectedValueOnce({ response: { status: 403, data: { detail: 'This company context is read-only' } } });
  fireEvent.click(screen.getByRole('button', { name: 'Review Café L-piece' }));
  await screen.findByText('This company context is read-only');
  expect(onSelect).not.toHaveBeenCalled();
});
