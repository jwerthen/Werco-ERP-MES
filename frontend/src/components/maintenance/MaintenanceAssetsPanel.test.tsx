import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import { MaintenanceAssetsPanel } from './MaintenanceAssetsPanel';
import type { MaintenanceAsset } from '../../types/maintenance';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getMaintenanceAssets: jest.fn(),
    createMaintenanceAsset: jest.fn(),
    updateMaintenanceAsset: jest.fn(),
    retireMaintenanceAsset: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const forklift: MaintenanceAsset = {
  id: 4,
  asset_tag: 'FL-01',
  name: 'Forklift 1',
  asset_type: 'forklift',
  manufacturer: 'Toyota',
  model: '8FGU25',
  serial_number: null,
  location: 'Dock',
  notes: null,
  is_active: true,
  version: 3,
  active_schedule_count: 2,
  open_work_order_count: 1,
};

const desktop = () => screen.getByTestId('data-table');

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getMaintenanceAssets.mockResolvedValue([forklift]);
});

describe('MaintenanceAssetsPanel', () => {
  it('lists assets with their schedule and open work-order counts', async () => {
    render(<MaintenanceAssetsPanel canEdit onChanged={jest.fn()} />);
    await waitFor(() => expect(within(desktop()).getByText('FL-01')).toBeInTheDocument());
    const row = within(desktop()).getByText('FL-01').closest('tr') as HTMLElement;
    expect(within(row).getByText('Forklift 1')).toBeInTheDocument();
    expect(within(row).getByText('2')).toBeInTheDocument();
    expect(within(row).getByText('1')).toBeInTheDocument();
    expect(mockedApi.getMaintenanceAssets).toHaveBeenCalledWith({ include_inactive: false });

    fireEvent.click(screen.getByLabelText('Show retired'));
    await waitFor(() => expect(mockedApi.getMaintenanceAssets).toHaveBeenLastCalledWith({ include_inactive: true }));
  });

  it('adds an asset and tells the page to re-read its pickers', async () => {
    mockedApi.createMaintenanceAsset.mockResolvedValue({ ...forklift, id: 5, asset_tag: 'AC-01' });
    const onChanged = jest.fn();
    render(<MaintenanceAssetsPanel canEdit onChanged={onChanged} />);
    await waitFor(() => expect(within(desktop()).getByText('FL-01')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /New Asset/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Asset Tag/), { target: { value: ' AC-01 ' } });
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'Air compressor' } });
    fireEvent.change(within(dialog).getByLabelText(/^Type/), { target: { value: 'air_compressor' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add Asset' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mockedApi.createMaintenanceAsset).toHaveBeenCalledWith({
      asset_tag: 'AC-01',
      name: 'Air compressor',
      asset_type: 'air_compressor',
      manufacturer: null,
      model: null,
      serial_number: null,
      location: null,
      notes: null,
    });
  });

  it('edits with the version CAS and only the changed fields; a 409 keeps the form open', async () => {
    mockedApi.updateMaintenanceAsset.mockRejectedValue({
      response: { status: 409, data: { detail: "An asset tagged 'FL-02' already exists" } },
    });
    render(<MaintenanceAssetsPanel canEdit onChanged={jest.fn()} />);
    await waitFor(() => expect(within(desktop()).getByText('FL-01')).toBeInTheDocument());
    fireEvent.click(within(desktop()).getByRole('button', { name: 'Edit asset FL-01' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Asset Tag/), { target: { value: 'FL-02' } });
    fireEvent.change(within(dialog).getByLabelText(/^Location/), { target: { value: '' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save Asset' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("An asset tagged 'FL-02' already exists");
    expect(mockedApi.updateMaintenanceAsset).toHaveBeenCalledWith(4, {
      version: 3,
      asset_tag: 'FL-02',
      location: null,
    });
  });

  it('retires through a confirmation, never a delete', async () => {
    mockedApi.retireMaintenanceAsset.mockResolvedValue({ ...forklift, is_active: false, version: 4 });
    render(<MaintenanceAssetsPanel canEdit onChanged={jest.fn()} />);
    await waitFor(() => expect(within(desktop()).getByText('FL-01')).toBeInTheDocument());
    fireEvent.click(within(desktop()).getByRole('button', { name: 'Retire asset FL-01' }));
    const confirm = await screen.findByRole('dialog');
    expect(confirm).toHaveTextContent('its 2 active schedule(s), work orders and history are kept');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Retire' }));
    await waitFor(() => expect(mockedApi.retireMaintenanceAsset).toHaveBeenCalledWith(4));
  });

  it('is read-only without a planning role', async () => {
    render(<MaintenanceAssetsPanel canEdit={false} onChanged={jest.fn()} />);
    await waitFor(() => expect(within(desktop()).getByText('FL-01')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /New Asset/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit asset/ })).not.toBeInTheDocument();
  });
});
