import React, { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import CarFormFields, {
  BLANK_CAR_FORM_VALUES,
  carFormPayload,
  carFormValuesFromRecord,
  enteredCostTotal,
  type CarFormValues,
} from './CarFormFields';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getParts: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getParts.mockResolvedValue([]);
  mocked.getCustomerNames.mockResolvedValue([{ id: 3, name: 'Acme Aerospace' }]);
  mocked.getVendors.mockResolvedValue([]);
});

describe('carFormPayload', () => {
  it('sends blanks as null, ids as numbers and money as the typed decimal string', () => {
    const payload = carFormPayload({
      ...BLANK_CAR_FORM_VALUES,
      initiation_reason: 'customer_feedback',
      part_id: '12',
      customer_id: '',
      vendor_id: ' 7 ',
      cost_labor: '125.50',
      cost_material: ' ',
      submitted_date: '2026-10-01',
    });
    expect(payload).toMatchObject({
      initiation_reason: 'customer_feedback',
      part_id: 12,
      customer_id: null,
      vendor_id: 7,
      cost_labor: '125.50',
      cost_material: null,
      submitted_date: '2026-10-01',
      rma_number: null,
    });
  });

  it('never sends the server-owned fields (customer name, cost total, quality approval)', () => {
    const payload = carFormPayload(BLANK_CAR_FORM_VALUES);
    for (const key of ['customer_name', 'cost_total', 'quality_approved_by', 'quality_approved_at']) {
      expect(payload).not.toHaveProperty(key);
    }
  });

  it('sends free text as typed, so an untouched value never reads as an edit', () => {
    const values = carFormValuesFromRecord({ management_action: 'Review at the monthly meeting.\n' });
    expect(carFormPayload(values).management_action).toBe('Review at the monthly meeting.\n');
  });

  it('round-trips a saved record (decimal strings, dates, reason)', () => {
    const values = carFormValuesFromRecord({
      initiation_reason: 'other',
      initiation_reason_other: 'Supplier survey',
      part_id: 12,
      cost_labor: '1250.00',
      vendor_approval_date: '2026-09-30',
    });
    expect(values).toMatchObject({
      initiation_reason: 'other',
      initiation_reason_other: 'Supplier survey',
      part_id: '12',
      cost_labor: '1250.00',
      vendor_approval_date: '2026-09-30',
    });
  });
});

describe('enteredCostTotal', () => {
  it('sums in cents and is null while nothing is entered', () => {
    expect(enteredCostTotal(BLANK_CAR_FORM_VALUES)).toBeNull();
    expect(
      enteredCostTotal({ ...BLANK_CAR_FORM_VALUES, cost_labor: '0.10', cost_material: '0.20', cost_shipping: '5' })
    ).toBe(5.3);
  });
});

function Harness({ initial = BLANK_CAR_FORM_VALUES }: { initial?: CarFormValues }) {
  const [values, setValues] = useState(initial);
  return (
    <>
      <CarFormFields
        values={values}
        onChange={patch => setValues(current => ({ ...current, ...patch }))}
        parts={[{ id: 12, part_number: 'PN-12', name: 'Bracket' }]}
      />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </>
  );
}

const current = () => JSON.parse(screen.getByTestId('values').textContent || '{}') as CarFormValues;

it('the Other box asks for its text, and leaving Other clears it', async () => {
  render(<Harness />);
  expect(screen.queryByLabelText(/^Other reason/)).not.toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('Reason for initiation'), { target: { value: 'other' } });
  fireEvent.change(screen.getByLabelText(/^Other reason/), { target: { value: 'Supplier survey' } });
  expect(current().initiation_reason_other).toBe('Supplier survey');

  fireEvent.change(screen.getByLabelText('Reason for initiation'), { target: { value: 'internal_audit' } });
  expect(screen.queryByLabelText(/^Other reason/)).not.toBeInTheDocument();
  expect(current()).toMatchObject({ initiation_reason: 'internal_audit', initiation_reason_other: '' });
  // The customer picker loads live customers only (the server refuses a retired one).
  expect(await screen.findByLabelText('Customer')).toBeInTheDocument();
  expect(mocked.getCustomerNames).toHaveBeenCalled();
  // Vendors include inactive ones: a supplier CAR can document the removal.
  expect(mocked.getVendors).toHaveBeenCalledWith({ active_only: false });
});

it('shows the typed cost total, labelled as such', () => {
  render(<Harness />);
  fireEvent.change(screen.getByLabelText('Labor cost'), { target: { value: '100' } });
  fireEvent.change(screen.getByLabelText('Shipping / transportation cost'), { target: { value: '25.5' } });
  expect(screen.getByTestId('car-cost-total')).toHaveTextContent('$125.50');
});
