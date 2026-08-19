/**
 * WorkOrderNew — the optional "Unit #" field, and the one thing about it that is a
 * contract rather than a form control.
 *
 * A BLANK unit number must be OMITTED from the create payload entirely, not sent as
 * `''`. The distinction is not cosmetic: `''` would persist an empty string into
 * `work_orders.unit_number`, which is a value the column can hold and which every read
 * surface then has to defend against separately — `UnitBadge` collapses it, but
 * `wallboard_service`'s `or None` and `WoCard`'s `trim() || null` are three independent
 * guards, and the searches would match `''` on a bare `%%` query. Absent means absent;
 * the server's `Optional[str] = None` is the shape that says so.
 *
 * The filled case is asserted trimmed for the same reason a leading space is not a
 * different unit — and because the value goes straight onto a TV at 31px.
 *
 * Mirrors the harness in WorkOrderNew.serials.test.tsx, which pins the same
 * omitted-when-blank property for the other optional identity field on this form.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { Part } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getParts: jest.fn(),
    getBOMs: jest.fn(),
    getWorkCenters: jest.fn(),
    getCustomerNames: jest.fn(),
    getPartReadiness: jest.fn(),
    getRoutingByPart: jest.fn(),
    previewWorkOrderOperations: jest.fn(),
    createWorkOrder: jest.fn(),
    createCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const PART: Part = {
  id: 1,
  version: 1,
  part_number: 'PN-7731',
  revision: 'A',
  name: 'Weldment, frame',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getParts.mockResolvedValue([PART]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getPartReadiness.mockResolvedValue({ ready: true, blockers: [], warnings: [], checks: {} });
  mockedApi.getRoutingByPart.mockResolvedValue(null);
  mockedApi.createWorkOrder.mockResolvedValue({ id: 42 });
});

async function renderPage() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/new']}>
        <WorkOrderNew />
      </MemoryRouter>
    </ToastProvider>
  );
  await screen.findByLabelText(/unit #/i);
}

async function selectPart() {
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'PN-7731' } });
  const option = await screen.findByRole('option', { name: /PN-7731/i });
  fireEvent.mouseDown(option);
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(1));
}

function setQuantity(value: string) {
  fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value } });
}

function setUnitNumber(value: string) {
  fireEvent.change(screen.getByLabelText(/unit #/i), { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: /create work order/i }));
}

describe('WorkOrderNew unit number', () => {
  it('omits the key ENTIRELY when the field is left blank', async () => {
    await renderPage();
    await selectPart();
    setQuantity('1');
    // Deliberately not touched — the state of the field for every job that does not
    // track a unit, which is most of them.

    submit();

    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    // `not.toHaveProperty`, not `toEqual(expect.objectContaining({unit_number: undefined}))`:
    // the assertion is about the KEY's absence, and `''` would satisfy a looser check.
    expect(mockedApi.createWorkOrder.mock.calls[0][0]).not.toHaveProperty('unit_number');
  });

  it('omits the key when the field holds only whitespace', async () => {
    // Same requirement, reached the way it actually happens: a planner tabs through the
    // field and leaves a stray space, or pastes a value and deletes it.
    await renderPage();
    await selectPart();
    setQuantity('1');
    setUnitNumber('   ');

    submit();

    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(mockedApi.createWorkOrder.mock.calls[0][0]).not.toHaveProperty('unit_number');
  });

  it('sends a filled unit number, trimmed', async () => {
    await renderPage();
    await selectPart();
    setQuantity('1');
    setUnitNumber('  2410048 ');

    submit();

    await waitFor(() =>
      expect(mockedApi.createWorkOrder).toHaveBeenCalledWith(
        expect.objectContaining({ unit_number: '2410048', quantity_ordered: 1 })
      )
    );
  });

  it('caps the input at the column length so an over-long value is refused at the key', async () => {
    // `maxLength={50}` mirrors `WorkOrderBase.unit_number` Field(max_length=50), which
    // mirrors String(50). Without it an over-long value comes back as a raw 422 after
    // the round-trip instead of simply not being typeable.
    await renderPage();

    expect(screen.getByLabelText(/unit #/i)).toHaveAttribute('maxlength', '50');
  });
});
