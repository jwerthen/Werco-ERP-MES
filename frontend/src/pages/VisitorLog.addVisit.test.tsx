/**
 * Coverage for the staff "Add visit" back-entry feature on the Visitor Log.
 *
 * Two surfaces:
 *  1. VisitorLog page — the "Add visit" trigger is ADMIN/MANAGER only (mirrors
 *     the server's require_role([ADMIN, MANAGER]) on POST /visitor-logs/manual),
 *     and a row that was back-entered by staff (entered_by_user_id != null)
 *     renders the "Staff entry" chip while a live station capture does not.
 *  2. VisitorManualEntryModal — client-side validation mirrors the backend
 *     VisitorManualEntryRequest (required fields, future/ordering time rules,
 *     purpose='other' note, safety acknowledgment), and a valid submit calls
 *     api.addManualVisit with UTC 'Z' timestamps then closes + refreshes.
 *
 * api and usePermissions are mocked so only the component behavior drives the
 * assertions (no AuthContext / real fetch / real router data needed).
 */

import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import VisitorLog from './VisitorLog';
import VisitorManualEntryModal from '../components/visitor/VisitorManualEntryModal';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVisitorLogs: jest.fn(),
    addManualVisit: jest.fn(),
  },
}));

// Role is varied per test; jest permits factory closure over `mock`-prefixed vars.
let mockRole = 'admin';
let mockSuper = false;
jest.mock('../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ role: mockRole, isSuperuser: mockSuper }),
  default: () => ({ role: mockRole, isSuperuser: mockSuper }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// A base VisitorLogResponse-shaped row; overrides tailor each test.
const baseRow = {
  id: 1,
  visitor_name: 'Jane Caller',
  visitor_company: 'Acme Supply',
  visitor_phone: null,
  host_name: null,
  host_user_id: null,
  purpose: 'meeting',
  purpose_note: null,
  safety_acknowledged: true,
  status: 'signed_out',
  signed_in_at: '2026-07-01T18:00:00Z',
  signed_out_at: '2026-07-01T19:00:00Z',
  signin_station_id: null,
  station_label: null,
  entered_by_user_id: null,
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <VisitorLog />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'admin';
  mockSuper = false;
  mockedApi.getVisitorLogs.mockResolvedValue({ items: [], total: 0 } as any);
});

// ===========================================================================
// 1. "Add visit" trigger visibility by role
// ===========================================================================

describe('VisitorLog "Add visit" button visibility', () => {
  it.each(['admin', 'manager'])('shows the Add visit button for %s', async (role) => {
    mockRole = role;
    renderPage();
    await waitFor(() => expect(mockedApi.getVisitorLogs).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: /add visit/i })).toBeInTheDocument();
  });

  it.each(['supervisor', 'viewer'])('hides the Add visit button for %s', async (role) => {
    mockRole = role;
    renderPage();
    await waitFor(() => expect(mockedApi.getVisitorLogs).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /add visit/i })).not.toBeInTheDocument();
  });
});

// ===========================================================================
// 2. StaffEntryChip: staff back-entry vs live station capture
// ===========================================================================

describe('VisitorLog StaffEntryChip', () => {
  it('renders the "Staff entry" chip for a row entered by staff', async () => {
    mockedApi.getVisitorLogs.mockResolvedValue({
      items: [{ ...baseRow, id: 10, visitor_name: 'Backdated Visitor', entered_by_user_id: 7 }],
      total: 1,
    } as any);
    renderPage();
    await screen.findAllByText('Backdated Visitor');
    expect(screen.getAllByText('Staff entry').length).toBeGreaterThan(0);
  });

  it('does NOT render the chip for a live station-captured row', async () => {
    mockedApi.getVisitorLogs.mockResolvedValue({
      items: [
        {
          ...baseRow,
          id: 11,
          visitor_name: 'Lobby Visitor',
          station_label: 'Lobby Tablet',
          entered_by_user_id: null,
        },
      ],
      total: 1,
    } as any);
    renderPage();
    await screen.findAllByText('Lobby Visitor');
    // The station label is shown; the staff-entry chip is not.
    expect(screen.getAllByText('Lobby Tablet').length).toBeGreaterThan(0);
    expect(screen.queryByText('Staff entry')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// 3. VisitorManualEntryModal — validation + submit
// ===========================================================================

describe('VisitorManualEntryModal validation', () => {
  const renderModal = () => {
    const onClose = jest.fn();
    const onSaved = jest.fn();
    const utils = render(<VisitorManualEntryModal open onClose={onClose} onSaved={onSaved} />);
    return { ...utils, onClose, onSaved };
  };

  // <Modal> portals to document.body, so the render `container` doesn't hold the
  // form — query the portaled inputs by their stable `name` off the document.
  const signedInInput = () => document.querySelector('input[name="signed_in_at"]') as HTMLInputElement;
  const signedOutInput = () => document.querySelector('input[name="signed_out_at"]') as HTMLInputElement;

  it('shows required errors when submitting an empty form', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('button', { name: /add visit/i }));

    expect(await screen.findByText(/visitor name is required/i)).toBeInTheDocument();
    expect(await screen.findByText(/sign-in date and time is required/i)).toBeInTheDocument();
    expect(await screen.findByText(/acknowledgment is required/i)).toBeInTheDocument();
    expect(mockedApi.addManualVisit).not.toHaveBeenCalled();
  });

  it('blocks a future sign-in time client-side', async () => {
    const user = userEvent.setup();
    renderModal();

    fireEvent.change(screen.getByPlaceholderText('Full name'), { target: { value: 'Future Person' } });
    fireEvent.change(signedInInput(), { target: { value: '2999-01-01T09:00' } });
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /add visit/i }));

    expect(await screen.findByText(/sign-in time must be in the past/i)).toBeInTheDocument();
    expect(mockedApi.addManualVisit).not.toHaveBeenCalled();
  });

  it('blocks a sign-out earlier than the sign-in time', async () => {
    const user = userEvent.setup();
    renderModal();

    fireEvent.change(screen.getByPlaceholderText('Full name'), { target: { value: 'Out Of Order' } });
    fireEvent.change(signedInInput(), { target: { value: '2020-06-01T10:00' } });
    fireEvent.change(signedOutInput(), { target: { value: '2020-06-01T09:00' } }); // before sign-in
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /add visit/i }));

    expect(await screen.findByText(/sign-out must be on or after the sign-in time/i)).toBeInTheDocument();
    expect(mockedApi.addManualVisit).not.toHaveBeenCalled();
  });

  it('requires a purpose note when the purpose is "Other"', async () => {
    const user = userEvent.setup();
    renderModal();

    fireEvent.change(screen.getByPlaceholderText('Full name'), { target: { value: 'Other Purpose' } });
    fireEvent.change(signedInInput(), { target: { value: '2020-01-02T09:00' } });
    await user.click(screen.getByRole('checkbox'));

    // Pick "Other" from the custom SelectField.
    await user.click(screen.getByRole('button', { name: 'Purpose' }));
    await user.click(await screen.findByRole('option', { name: /other/i }));

    await user.click(screen.getByRole('button', { name: /add visit/i }));

    expect(await screen.findByText(/note is required when the purpose is/i)).toBeInTheDocument();
    expect(mockedApi.addManualVisit).not.toHaveBeenCalled();
  });

  it('requires the safety acknowledgment', async () => {
    const user = userEvent.setup();
    renderModal();

    fireEvent.change(screen.getByPlaceholderText('Full name'), { target: { value: 'No Ack' } });
    fireEvent.change(signedInInput(), { target: { value: '2020-01-02T09:00' } });
    // Deliberately leave the acknowledgment unchecked.
    await user.click(screen.getByRole('button', { name: /add visit/i }));

    expect(await screen.findByText(/acknowledgment is required/i)).toBeInTheDocument();
    expect(mockedApi.addManualVisit).not.toHaveBeenCalled();
  });

  it('submits a valid back-entry with UTC "Z" timestamps then closes + refreshes', async () => {
    const user = userEvent.setup();
    mockedApi.addManualVisit.mockResolvedValue({ id: 99, visitor_name: 'Jane Caller' } as any);
    const { onClose, onSaved } = renderModal();

    fireEvent.change(screen.getByPlaceholderText('Full name'), { target: { value: 'Jane Caller' } });
    fireEvent.change(signedInInput(), { target: { value: '2020-01-02T09:00' } });
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /add visit/i }));

    await waitFor(() => expect(mockedApi.addManualVisit).toHaveBeenCalledTimes(1));

    const payload = mockedApi.addManualVisit.mock.calls[0][0];
    expect(payload.visitor_name).toBe('Jane Caller');
    // The Central wall-clock is converted to a UTC ISO string with a trailing Z.
    expect(payload.signed_in_at).toMatch(/Z$/);
    expect(payload.signed_in_at).not.toContain('+');
    expect(payload.purpose).toBe('meeting');
    expect(payload.safety_acknowledged).toBe(true);

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });
});
