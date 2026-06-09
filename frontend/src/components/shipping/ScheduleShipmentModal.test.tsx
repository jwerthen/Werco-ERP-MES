/**
 * ScheduleShipmentModal — the multi-carrier Schedule-Shipment wizard.
 *
 * Covers the behavior the stage adds: the egress-disabled (HTTP 409) inline
 * banner with the Admin > Carriers CTA (NOT a raw error), the parcel
 * rate-shop -> rate-comparison -> buy-label happy path, and the graceful
 * HTTP 501 handling for freight buy-bol.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import ScheduleShipmentModal from './ScheduleShipmentModal';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    validateAddress: jest.fn(),
    rateShop: jest.fn(),
    buyLabel: jest.fn(),
    buyBol: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & { response: { status: number; data: { detail?: string } } };
  err.response = { status, data: { detail } };
  return err;
};

const target = {
  shipment_id: 42,
  shipment_number: 'SHP-20260609-001',
  work_order_number: 'WO-100',
  customer_name: 'Acme Aero',
};

const renderModal = (props: Partial<React.ComponentProps<typeof ScheduleShipmentModal>> = {}) =>
  render(
    <BrowserRouter>
      <ToastProvider>
        <ScheduleShipmentModal target={target} onClose={jest.fn()} onCompleted={jest.fn()} {...props} />
      </ToastProvider>
    </BrowserRouter>,
  );

const fillParcelAndAddress = () => {
  // Packages step: fill the single parcel row.
  const numberInputs = screen.getAllByRole('spinbutton');
  fireEvent.change(numberInputs[0], { target: { value: '6' } }); // length
  fireEvent.change(numberInputs[1], { target: { value: '6' } }); // width
  fireEvent.change(numberInputs[2], { target: { value: '6' } }); // height
  fireEvent.change(numberInputs[3], { target: { value: '2' } }); // weight
  fireEvent.click(screen.getByRole('button', { name: /continue/i }));

  // Address step: fill required ship-to fields.
  fireEvent.change(screen.getByLabelText('Street 1'), { target: { value: '1 Aero Way' } });
  fireEvent.change(screen.getByLabelText('City'), { target: { value: 'Tulsa' } });
  fireEvent.change(screen.getByLabelText('State'), { target: { value: 'OK' } });
  fireEvent.change(screen.getByLabelText('ZIP'), { target: { value: '74101' } });
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('ScheduleShipmentModal', () => {
  it('surfaces an egress-disabled banner with the Admin > Carriers CTA on HTTP 409 (not a raw error)', async () => {
    mockApi.rateShop.mockRejectedValueOnce(http(409, 'Carrier egress is disabled for this company.'));
    renderModal();

    fillParcelAndAddress();
    fireEvent.click(screen.getByRole('button', { name: /get rates/i }));

    await waitFor(() => expect(screen.getByText(/carrier egress is turned off/i)).toBeInTheDocument());
    expect(screen.getByText(/Carrier egress is disabled for this company\./i)).toBeInTheDocument();
    const cta = screen.getByRole('link', { name: /enable carrier egress in admin/i });
    expect(cta).toHaveAttribute('href', '/admin/settings?tab=carriers');
  });

  it('rate-shops, renders the comparison table, and buys the selected parcel label', async () => {
    mockApi.rateShop.mockResolvedValueOnce([
      {
        id: 1,
        provider_rate_id: 'rate_fast',
        carrier: 'FedEx',
        service_name: 'Priority Overnight',
        mode: 'parcel',
        amount: '45.00',
        currency: 'USD',
        est_delivery_days: 1,
        is_selected: false,
      },
      {
        id: 2,
        provider_rate_id: 'rate_cheap',
        carrier: 'USPS',
        service_name: 'Ground Advantage',
        mode: 'parcel',
        amount: '12.50',
        currency: 'USD',
        est_delivery_days: 4,
        is_selected: false,
      },
    ]);
    mockApi.buyLabel.mockResolvedValueOnce({
      shipment_id: 42,
      shipment_number: 'SHP-20260609-001',
      carrier: 'USPS',
      service_code: 'GroundAdvantage',
      tracking_number: '9400100000000000000000',
      actual_cost: '12.50',
      cost_currency: 'USD',
      label_document_id: 777,
      already_purchased: false,
    });
    const onCompleted = jest.fn();
    renderModal({ onCompleted });

    fillParcelAndAddress();
    fireEvent.click(screen.getByRole('button', { name: /get rates/i }));

    await waitFor(() => expect(screen.getByText('FedEx')).toBeInTheDocument());
    expect(screen.getByText('USPS')).toBeInTheDocument();
    expect(screen.getByText('45.00 USD')).toBeInTheDocument();

    // Select the cheaper USPS rate, then buy.
    const uspsRow = screen.getByText('USPS').closest('tr')!;
    fireEvent.click(within(uspsRow).getByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: /buy label/i }));

    await waitFor(() => expect(mockApi.buyLabel).toHaveBeenCalledWith(42, { rate_id: 'rate_cheap' }));
    expect(onCompleted).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText('9400100000000000000000')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /print label/i })).toBeEnabled();
  });

  it('handles HTTP 501 from buy-bol gracefully (freight not enabled), no crash', async () => {
    mockApi.rateShop.mockResolvedValueOnce([
      {
        id: 3,
        provider_rate_id: 'freight_1',
        carrier: 'FedEx Freight',
        service_name: 'LTL Priority',
        mode: 'freight',
        amount: '320.00',
        currency: 'USD',
        is_selected: false,
      },
    ]);
    mockApi.buyBol.mockRejectedValueOnce(http(501, 'Freight not supported by this provider'));
    renderModal();

    // Switch to Freight, fill the pallet row.
    fireEvent.click(screen.getByRole('button', { name: /freight \/ ltl/i }));
    const numberInputs = screen.getAllByRole('spinbutton');
    fireEvent.change(numberInputs[0], { target: { value: '48' } });
    fireEvent.change(numberInputs[1], { target: { value: '40' } });
    fireEvent.change(numberInputs[2], { target: { value: '50' } });
    fireEvent.change(numberInputs[3], { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    fireEvent.change(screen.getByLabelText('Street 1'), { target: { value: '1 Dock Rd' } });
    fireEvent.change(screen.getByLabelText('City'), { target: { value: 'Tulsa' } });
    fireEvent.change(screen.getByLabelText('State'), { target: { value: 'OK' } });
    fireEvent.change(screen.getByLabelText('ZIP'), { target: { value: '74101' } });
    fireEvent.click(screen.getByRole('button', { name: /get rates/i }));

    await waitFor(() => expect(screen.getByText('FedEx Freight')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: /buy bol/i }));

    await waitFor(() => expect(mockApi.buyBol).toHaveBeenCalled());
    // Graceful: still on the rates step, no purchase-result tracking number shown.
    expect(screen.getByText('FedEx Freight')).toBeInTheDocument();
  });
});
