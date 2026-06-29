/**
 * Batch 9 a11y — icon-only action buttons on the Users page now carry an
 * accessible name (aria-label) instead of being announced as just "button"
 * with a hidden SVG. These assertions resolve buttons BY ROLE + NAME, which
 * only succeeds because the aria-label landed; before the change the icons
 * were unlabeled (title attributes are not an accessible name for buttons
 * with no text content) and getByRole({ name }) would not match.
 *
 * The Users list renders a desktop <table> and a parallel mobile-card list
 * (DataTable.mobileCards), so each row's actions appear twice in jsdom —
 * assert with getAllByRole and expect a non-empty set.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Users from './Users';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getUsers: jest.fn(),
    getPendingUserApprovals: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string; is_superuser: boolean } = {
  id: 99,
  role: 'admin',
  is_superuser: false,
};

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const ACTIVE_USER = {
  id: 1,
  email: 'rosa@werco.test',
  employee_id: '40231',
  first_name: 'Rosa',
  last_name: 'Vega',
  role: 'operator',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
};

const INACTIVE_USER = {
  id: 2,
  email: 'sam@werco.test',
  employee_id: '40232',
  first_name: 'Sam',
  last_name: 'Lee',
  role: 'quality',
  is_active: false,
  created_at: '2026-01-01T00:00:00Z',
};

function renderUsers() {
  return render(
    <MemoryRouter initialEntries={['/users']}>
      <Users />
    </MemoryRouter>
  );
}

describe('Users page — icon-only action buttons have accessible names', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockAuthUser = { id: 99, role: 'admin', is_superuser: false };
    mockedApi.getUsers.mockResolvedValue([ACTIVE_USER]);
    mockedApi.getPendingUserApprovals.mockResolvedValue([]);
  });

  it('exposes the edit-user action by accessible name (aria-label landed)', async () => {
    renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    // Resolves only because the icon button now has aria-label="Edit user".
    const editButtons = screen.getAllByRole('button', { name: /edit user/i });
    expect(editButtons.length).toBeGreaterThan(0);
  });

  it('exposes the reset-password action by accessible name', async () => {
    renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    expect(screen.getAllByRole('button', { name: /reset password/i }).length).toBeGreaterThan(0);
  });

  it('labels the activate/deactivate toggle by its current state', async () => {
    renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    // Active user => the toggle deactivates; its label reflects the action.
    // Anchor exactly: "activate user" is a substring of "deactivate user", so
    // an unanchored /activate user/ would falsely match the deactivate button.
    expect(screen.getAllByRole('button', { name: /^deactivate user$/i }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /^activate user$/i })).not.toBeInTheDocument();
  });

  it('flips the toggle label to "Activate user" for an inactive user', async () => {
    mockedApi.getUsers.mockResolvedValue([INACTIVE_USER]);
    renderUsers();
    // Inactive users only show when "Show inactive users" is on; the default
    // load (includeInactive=false) returns our mock regardless, so the row
    // renders and we can read its toggle label.
    await waitFor(() => expect(screen.getAllByText('Sam Lee').length).toBeGreaterThan(0));

    expect(screen.getAllByRole('button', { name: /^activate user$/i }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /^deactivate user$/i })).not.toBeInTheDocument();
  });

  it('marks the action icons aria-hidden so they are not double-announced', async () => {
    const { container } = renderUsers();
    await waitFor(() => expect(screen.getAllByText('Rosa Vega').length).toBeGreaterThan(0));

    // Every SVG inside a labeled action button is decorative (aria-hidden),
    // so the button announces its label, not the icon.
    const editButton = screen.getAllByRole('button', { name: /edit user/i })[0];
    const svg = editButton.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    // Sanity: container is mounted (guards against an empty render passing).
    expect(container).not.toBeEmptyDOMElement();
  });
});
