/**
 * Quality — NCR Void action.
 *
 * Covers the NCR-void feature wired into the Quality page:
 *  - a Void action opens the modal, which requires a reason (blank is blocked
 *    client-side) and on confirm calls api.voidNCR(id, reason);
 *  - a server refusal surfaces the verbatim `detail` in an error toast;
 *  - RBAC parity with the backend gate: the Void control only renders for a user
 *    holding `quality:approve` (admin/manager/quality), and is hidden otherwise.
 *
 * usePermissions + the api service are mocked at the module boundary; the real
 * ToastProvider wraps the page so toast text is assertable (sibling-test pattern).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import Quality from './Quality';

// `can` is mutated per test to flip the quality:approve gate.
let mockCan: (perm: string) => boolean = () => true;
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: (p: string) => mockCan(p), canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    voidNCR: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const openNcr = {
  id: 1,
  ncr_number: 'NCR-0001',
  quantity_affected: 2,
  source: 'in_process',
  status: 'open',
  disposition: 'pending',
  title: 'Surface scratch',
  description: 'Scratch on face',
  created_at: '2026-06-20T12:00:00Z',
};

function renderQuality() {
  return render(
    <MemoryRouter initialEntries={['/quality']}>
      <ToastProvider>
        <Quality />
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockCan = () => true;
  mockedApi.getNCRs.mockResolvedValue([openNcr] as any);
  mockedApi.getCARs.mockResolvedValue([] as any);
  mockedApi.getFAIs.mockResolvedValue([] as any);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 1, open_cars: 0, pending_fais: 0 } as any);
  mockedApi.getParts.mockResolvedValue([] as any);
});

// The NCR list renders a desktop table AND mobile cards, so the row Void button
// (aria-label "Void NCR NCR-0001") appears twice; either opens the same modal.
const openVoidModal = async () => {
  await waitFor(() =>
    expect(screen.getAllByRole('button', { name: /Void NCR NCR-0001/i }).length).toBeGreaterThan(0),
  );
  fireEvent.click(screen.getAllByRole('button', { name: /Void NCR NCR-0001/i })[0]);
  await screen.findByRole('heading', { name: /Void NCR NCR-0001/i });
};

describe('Quality — NCR void', () => {
  it('requires a reason before calling api.voidNCR', async () => {
    renderQuality();
    await openVoidModal();

    // The modal submit button's accessible name is exactly "Void NCR" (the row
    // triggers are "Void NCR NCR-0001").
    fireEvent.click(screen.getByRole('button', { name: 'Void NCR' }));
    expect(await screen.findByText(/a reason is required to void an NCR/i)).toBeInTheDocument();
    expect(mockedApi.voidNCR).not.toHaveBeenCalled();
  });

  it('calls api.voidNCR(id, reason) on confirm', async () => {
    mockedApi.voidNCR.mockResolvedValueOnce({ message: 'NCR NCR-0001 voided', can_restore: true });
    renderQuality();
    await openVoidModal();

    fireEvent.change(screen.getByLabelText(/Reason for Void/i), {
      target: { value: 'Raised in error — duplicate of NCR-0002.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Void NCR' }));

    await waitFor(() =>
      expect(mockedApi.voidNCR).toHaveBeenCalledWith(1, 'Raised in error — duplicate of NCR-0002.'),
    );
  });

  it('surfaces the verbatim server detail on a refusal (e.g. blocking a work order)', async () => {
    const detail = 'Cannot void NCR NCR-0001: it is blocking work order(s). Resolve the blocker first.';
    mockedApi.voidNCR.mockRejectedValueOnce(http(400, detail));
    renderQuality();
    await openVoidModal();

    fireEvent.change(screen.getByLabelText(/Reason for Void/i), { target: { value: 'try' } });
    fireEvent.click(screen.getByRole('button', { name: 'Void NCR' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  it('hides the Void control without quality:approve', async () => {
    mockCan = (perm: string) => perm !== 'quality:approve';
    renderQuality();
    // The NCR row renders; the Void trigger must not.
    await waitFor(() => expect(screen.getAllByText('NCR-0001').length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: /Void NCR/i })).toBeNull();
  });
});
