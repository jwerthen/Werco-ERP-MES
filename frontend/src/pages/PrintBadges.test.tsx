/**
 * A0.4 badge print sheet — /print/badges?user_ids=...
 *
 * Locks: selected users render as CR80 cards with name, role label, employee id,
 * and a QR whose payload is the STORED employee_id verbatim (what
 * /auth/employee-login and /scanner/resolve-action expect); unrequested users
 * do not render; an empty selection shows guidance instead of crashing.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import QRCode from 'qrcode';
import api from '../services/api';
import PrintBadges from './PrintBadges';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
  },
}));

jest.mock('qrcode', () => ({
  __esModule: true,
  default: {
    toDataURL: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedToDataURL = QRCode.toDataURL as jest.Mock;

const USERS = [
  {
    id: 1,
    employee_id: '40231',
    first_name: 'Rosa',
    last_name: 'Vega',
    role: 'operator',
    department: 'Welding',
    is_active: true,
  },
  {
    id: 2,
    employee_id: 'EMP-00339',
    first_name: 'Sam',
    last_name: 'Lee',
    role: 'quality',
    is_active: true,
  },
  {
    id: 3,
    employee_id: '99999',
    first_name: 'Not',
    last_name: 'Selected',
    role: 'viewer',
    is_active: true,
  },
];

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/print/badges" element={<PrintBadges />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('PrintBadges', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getUsers.mockResolvedValue(USERS);
    mockedToDataURL.mockImplementation(async (payload: string) => `data:image/png;base64,${payload}`);
  });

  it('renders one CR80 card per selected user with name, role, and employee id', async () => {
    renderAt('/print/badges?user_ids=1,2');

    await waitFor(() => expect(screen.getByText('Rosa Vega')).toBeInTheDocument());
    expect(screen.getByText('Sam Lee')).toBeInTheDocument();
    expect(screen.getByText('Operator')).toBeInTheDocument();
    expect(screen.getByText('Quality')).toBeInTheDocument();
    expect(screen.getByText('ID: 40231')).toBeInTheDocument();
    expect(screen.getByText('ID: EMP-00339')).toBeInTheDocument();

    // Unrequested user never renders.
    expect(screen.queryByText('Not Selected')).not.toBeInTheDocument();

    expect(screen.getByTestId('badge-1')).toBeInTheDocument();
    expect(screen.getByTestId('badge-2')).toBeInTheDocument();
    expect(screen.queryByTestId('badge-3')).not.toBeInTheDocument();
  });

  it('encodes the stored employee_id verbatim in the badge QR', async () => {
    renderAt('/print/badges?user_ids=1,2');

    await waitFor(() => expect(mockedToDataURL).toHaveBeenCalledTimes(2));
    expect(mockedToDataURL).toHaveBeenCalledWith('40231', expect.any(Object));
    expect(mockedToDataURL).toHaveBeenCalledWith('EMP-00339', expect.any(Object));

    await waitFor(() => expect(screen.getByAltText('Badge code for 40231')).toBeInTheDocument());
    expect(screen.getByAltText('Badge code for EMP-00339')).toHaveAttribute(
      'src',
      'data:image/png;base64,EMP-00339'
    );
  });

  it('shows guidance when no users are selected and does not call the API', async () => {
    renderAt('/print/badges');

    await waitFor(() =>
      expect(
        screen.getByText('No users selected. Open this page from the Users screen via "Print badges".')
      ).toBeInTheDocument()
    );
    expect(mockedApi.getUsers).not.toHaveBeenCalled();
  });

  it('reports when none of the requested users exist', async () => {
    renderAt('/print/badges?user_ids=777');

    await waitFor(() =>
      expect(screen.getByText('None of the requested users were found.')).toBeInTheDocument()
    );
  });
});
