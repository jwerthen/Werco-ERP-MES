/**
 * Timezone-consistency regression guard (frontend display side).
 *
 * The trigger bug: a sign-out stored 19:17 UTC and, when the wire value lacked
 * a trailing 'Z', the old display formatter parsed it as viewer-local and
 * rendered "7:17 PM" instead of the shop's Central "2:17 PM". VisitorLog now
 * routes every timestamp through `formatCentralDateTime` (America/Chicago), and
 * `centralTime.toDate` treats a zone-less backend string as UTC.
 *
 * This test renders VisitorLog with a row whose `signed_out_at` is the exact
 * no-'Z' string `"2026-07-01T19:17:00"` and asserts the rendered cell shows the
 * Central time (2:17 PM), NOT 7:17. On 2026-07-01 Central is CDT (UTC-5), so
 * 19:17 UTC → 14:17 = 2:17 PM.
 *
 * api and usePermissions are mocked so only the display path drives the
 * assertion (no AuthContext / real fetch needed).
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import VisitorLog from './VisitorLog';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVisitorLogs: jest.fn(),
  },
}));

// Force a deterministic viewer role (ADMIN) without an AuthContext provider.
jest.mock('../hooks/usePermissions', () => ({
  __esModule: true,
  usePermissions: () => ({ role: 'admin', isSuperuser: false }),
  default: () => ({ role: 'admin', isSuperuser: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// A signed-out visitor whose timestamps are naive-UTC (no 'Z' on the wire) —
// the shape that regressed. 19:17 UTC must render as Central 2:17 PM.
const signedOutRow = {
  id: 1,
  visitor_name: 'Zulu Visitor',
  visitor_company: 'Acme Supply',
  visitor_phone: null,
  host_name: null,
  host_user_id: null,
  purpose: 'meeting',
  purpose_note: null,
  safety_acknowledged: true,
  status: 'signed_out',
  signed_in_at: '2026-07-01T18:00:00',
  signed_out_at: '2026-07-01T19:17:00',
  signin_station_id: null,
  station_label: null,
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <VisitorLog />
    </MemoryRouter>
  );

describe('VisitorLog timezone display', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getVisitorLogs.mockResolvedValue({ items: [signedOutRow], total: 1 } as any);
  });

  it('renders a no-Z sign-out timestamp in Central time (2:17 PM), not viewer-local 7:17', async () => {
    renderPage();

    // Wait for the mocked fetch to resolve and the row to render. The name
    // appears in both the desktop table row and the responsive mobile card,
    // so use findAllByText (findByText throws on multiple matches).
    await waitFor(() => expect(mockedApi.getVisitorLogs).toHaveBeenCalled());
    await screen.findAllByText('Zulu Visitor');

    // The signed-out cell shows Central 2:17 PM (19:17 UTC − 5h CDT), never 7:17.
    // The timestamp is embedded in a "Jul 1, 2026, 2:17 PM" node, so match the
    // substring via regex.
    const centralCells = screen.getAllByText(/2:17\s?PM/);
    expect(centralCells.length).toBeGreaterThan(0);
    expect(screen.queryByText(/7:17\s?PM/)).not.toBeInTheDocument();
  });
});
