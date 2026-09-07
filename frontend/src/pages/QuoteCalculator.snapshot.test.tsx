import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import QuoteCalculator from './QuoteCalculator';
import api from '../services/api';
const navigate = jest.fn();
jest.mock('react-router-dom', () => ({ ...jest.requireActual('react-router-dom'), useNavigate: () => navigate }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 8, company_id: 1 } }) }));
jest.mock('../components/DXFViewer', () => () => null);
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuoteMaterials: jest.fn(),
    getQuoteFinishes: jest.fn(),
    calculateCNCQuote: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;
const result = {
  total: 200,
  unit_price: 20,
  lead_time_days: 7,
  estimated_hours: 2,
  material_cost: 20,
  cutting_cost: 0,
  machining_cost: 10,
  setup_cost: 0,
  bending_cost: 0,
  hardware_cost: 0,
  finish_cost: 0,
  unit_cost: 3,
  subtotal: 30,
  markup_amount: 170,
  quantity_discount: 0,
  rush_charge: 0,
  details: {},
};
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  mockedApi.getQuoteMaterials.mockResolvedValue([{ id: 1, name: 'Steel', category: 'steel' }]);
  mockedApi.getQuoteFinishes.mockResolvedValue([]);
  mockedApi.calculateCNCQuote.mockResolvedValue(result);
});

test('edited inputs mark prices stale and keep the quantity belonging to the result', async () => {
  render(<QuoteCalculator />);
  const qty = await screen.findByLabelText('Quantity');
  fireEvent.change(qty, { target: { value: '10' } });
  fireEvent.click(screen.getByRole('button', { name: /Calculate Quote/i }));
  await screen.findByText('$200.00');
  fireEvent.change(qty, { target: { value: '20' } });
  expect(screen.getByText(/Inputs changed/)).toBeInTheDocument();
  expect(screen.getByText(/per unit x 10/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Create Quote' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Print' })).toBeDisabled();
});

test('handoff carries the exact calculation and returning restores the draft in StrictMode', async () => {
  const view = render(
    <React.StrictMode>
      <QuoteCalculator />
    </React.StrictMode>
  );
  fireEvent.change(await screen.findByLabelText('Quantity'), { target: { value: '10' } });
  fireEvent.click(screen.getByRole('button', { name: /Calculate Quote/i }));
  await screen.findByText('$200.00');
  fireEvent.click(screen.getByRole('button', { name: 'Create Quote' }));
  expect(navigate).toHaveBeenCalledWith(
    '/quotes?create=calculator',
    expect.objectContaining({
      state: expect.objectContaining({
        calculatorDraft: expect.objectContaining({
          lead_time_days: 7,
          lines: [expect.objectContaining({ quantity: 10, unit_price: 20 })],
        }),
      }),
    })
  );
  view.unmount();
  render(
    <React.StrictMode>
      <QuoteCalculator />
    </React.StrictMode>
  );
  await waitFor(() => expect(screen.getByLabelText('Quantity')).toHaveValue(10));
  expect(await screen.findByText('$200.00')).toBeInTheDocument();
});

test('quote total reconciles the rounded unit price and names its rounding adjustment', async () => {
  mockedApi.calculateCNCQuote.mockResolvedValue({ ...result, total: 908.12, unit_price: 45.41 });
  render(<QuoteCalculator />);
  fireEvent.change(await screen.findByLabelText('Quantity'), { target: { value: '20' } });
  fireEvent.click(screen.getByRole('button', { name: /Calculate Quote/i }));
  expect(await screen.findByText('$908.20')).toBeInTheDocument();
  expect(screen.getByText('Unit-price rounding')).toBeInTheDocument();
  expect(screen.getByText('+$0.08')).toBeInTheDocument();
  expect(screen.queryByText('Markup (25%)')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Create Quote' }));
  expect(navigate.mock.calls[0][1].state.calculatorDraft.lines[0]).toEqual(
    expect.objectContaining({ quantity: 20, unit_price: 45.41 })
  );
});
