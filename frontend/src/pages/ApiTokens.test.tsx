/**
 * Admin → API Tokens (`/admin/api-tokens`).
 *
 * Locks the contract the page owes the backend's `/api-tokens` verbs:
 *   1. the list shows holder, scope, the AI-external badge, JTI, last used,
 *      expiry and status, and the "Show revoked" filter is URL state that is
 *      passed to the server;
 *   2. the issue form defaults to read-only + AI-external, forces AI-external
 *      off (and disabled) for a full token, sends exactly what was chosen, and
 *      offers only users the server would accept;
 *   3. the issued token is revealed once — with a "shown once" warning, a copy
 *      button, and no write to browser storage or the console — and is gone
 *      from the DOM after "Done";
 *   4. issue and revoke are non-optimistic: a refusal keeps the dialog open and
 *      toasts the server's detail; revoke requires a reason;
 *   5. the AI-egress-off banner appears when the company switch is off.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import api from '../services/api';
import ApiTokens from './ApiTokens';
import { ToastProvider } from '../components/ui/Toast';
import { comboBoxOptions, openComboBox, selectComboBoxOption } from '../test-utils/comboBox';
import type { ApiToken, ApiTokenIssued } from '../types/apiToken';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    listApiTokens: jest.fn(),
    createApiToken: jest.fn(),
    revokeApiToken: jest.fn(),
    getUsers: jest.fn(),
    getCurrentCompany: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const USERS = [
  {
    id: 10,
    email: 'assistant@werco.test',
    first_name: 'Werco',
    last_name: 'Assistant',
    role: 'viewer',
    is_active: true,
    is_superuser: false,
  },
  {
    id: 11,
    email: 'ops@werco.test',
    first_name: 'Olive',
    last_name: 'Ops',
    role: 'manager',
    is_active: true,
    is_superuser: false,
  },
  {
    id: 12,
    email: 'gone@werco.test',
    first_name: 'Gone',
    last_name: 'Person',
    role: 'operator',
    is_active: false,
    is_superuser: false,
  },
  {
    id: 13,
    email: 'platform@werco.test',
    first_name: 'Plat',
    last_name: 'Form',
    role: 'platform_admin',
    is_active: true,
    is_superuser: false,
  },
  {
    id: 14,
    email: 'root@werco.test',
    first_name: 'Root',
    last_name: 'User',
    role: 'admin',
    is_active: true,
    is_superuser: true,
  },
];

const readOnlyToken: ApiToken = {
  id: 1,
  label: 'Claude connector',
  user_id: 10,
  jti_prefix: 'ab12cd34',
  scope: 'read_only',
  ai_external: true,
  expires_at: null,
  revoked: false,
  revoked_at: null,
  revoked_by: null,
  revoke_reason: null,
  last_used_at: null,
  created_by: 1,
  created_at: '2026-11-01T15:00:00Z',
};

const fullToken: ApiToken = {
  ...readOnlyToken,
  id: 2,
  label: 'Grok bot',
  user_id: 11,
  jti_prefix: 'ff00ee11',
  scope: 'full',
  ai_external: false,
  expires_at: '2099-01-01T00:00:00Z',
  last_used_at: '2026-11-12T18:30:00Z',
};

const revokedToken: ApiToken = {
  ...readOnlyToken,
  id: 3,
  label: 'Old bot',
  revoked: true,
  revoked_at: '2026-11-05T12:00:00Z',
  revoked_by: 1,
  revoke_reason: 'Rotated',
};

const SECRET = 'eyJhbGciOi.secret-token.value';

const issued: ApiTokenIssued = {
  ...readOnlyToken,
  id: 9,
  label: 'New connector',
  jti_prefix: '99887766',
  token: SECRET,
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

function renderPage(entry = '/admin/api-tokens') {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route
            path="/admin/api-tokens"
            element={
              <>
                <ApiTokens />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

function tableRow(label: string): HTMLElement {
  const row = screen.getByText(label).closest('tr');
  if (!row) throw new Error(`No row for ${label}`);
  return row as HTMLElement;
}

async function openIssueDialog() {
  fireEvent.click(await screen.findByRole('button', { name: /issue token/i }));
  return screen.findByRole('dialog', { name: /issue api token/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listApiTokens.mockResolvedValue([readOnlyToken, fullToken]);
  mockApi.getUsers.mockResolvedValue(USERS);
  mockApi.getCurrentCompany.mockResolvedValue({ id: 1, allow_ai_egress: true });
});

describe('API Tokens page — list', () => {
  it('shows holder, scope, AI-external badge, JTI, last used, expiry and status', async () => {
    renderPage();

    await screen.findByText('Claude connector');
    const ro = tableRow('Claude connector');
    expect(within(ro).getByText('Werco Assistant')).toBeInTheDocument();
    expect(within(ro).getByText('Read-only')).toBeInTheDocument();
    expect(within(ro).getByText('AI-external')).toBeInTheDocument();
    expect(within(ro).getByText('ab12cd34')).toBeInTheDocument();
    // Never used, never expires.
    expect(within(ro).getAllByText('Never')).toHaveLength(2);
    expect(within(ro).getByText('active')).toBeInTheDocument();

    const full = tableRow('Grok bot');
    expect(within(full).getByText('Olive Ops')).toBeInTheDocument();
    expect(within(full).getByText('Full')).toBeInTheDocument();
    expect(within(full).queryByText('AI-external')).not.toBeInTheDocument();
    // Last used renders in Central time, never "Never".
    expect(within(full).getByText(/Nov 12, 2026/)).toBeInTheDocument();
    expect(within(full).getByRole('button', { name: 'Revoke Grok bot' })).toBeInTheDocument();

    expect(mockApi.listApiTokens).toHaveBeenCalledWith({ includeRevoked: false });
  });

  it('keeps "Show revoked" in the URL and asks the server for revoked rows', async () => {
    mockApi.listApiTokens.mockImplementation(async params =>
      params?.includeRevoked ? [readOnlyToken, revokedToken] : [readOnlyToken]
    );
    renderPage();
    await screen.findByText('Claude connector');
    expect(screen.queryByText('Old bot')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Show revoked tokens'));

    await screen.findByText('Old bot');
    expect(mockApi.listApiTokens).toHaveBeenLastCalledWith({ includeRevoked: true });
    expect(screen.getByTestId('location')).toHaveTextContent('?revoked=1');
    const revokedRow = tableRow('Old bot');
    expect(within(revokedRow).getByText('revoked')).toBeInTheDocument();
    expect(within(revokedRow).getByText(/Rotated/)).toBeInTheDocument();
    // A revoke is one-way: no button on a revoked row.
    expect(within(revokedRow).queryByRole('button', { name: /revoke/i })).not.toBeInTheDocument();
  });

  it('reads "Show revoked" back from the URL on load', async () => {
    mockApi.listApiTokens.mockResolvedValue([revokedToken]);
    renderPage('/admin/api-tokens?revoked=1');
    await screen.findByText('Old bot');
    expect(screen.getByLabelText('Show revoked tokens')).toBeChecked();
    expect(mockApi.listApiTokens).toHaveBeenCalledWith({ includeRevoked: true });
  });

  it('warns that AI-external tokens are refused while company AI egress is off', async () => {
    mockApi.getCurrentCompany.mockResolvedValue({ id: 1, allow_ai_egress: false });
    renderPage();
    const banner = await screen.findByTestId('api-tokens-egress-off');
    expect(banner).toHaveTextContent(/AI egress is off/i);
    expect(within(banner).getByRole('link', { name: /AI Privacy/ })).toHaveAttribute(
      'href',
      '/admin/settings?tab=aiprivacy'
    );
  });

  it('shows no egress banner when the switch is on', async () => {
    renderPage();
    await screen.findByText('Claude connector');
    await waitFor(() => expect(mockApi.getCurrentCompany).toHaveBeenCalled());
    expect(screen.queryByTestId('api-tokens-egress-off')).not.toBeInTheDocument();
  });
});

describe('API Tokens page — issue', () => {
  it('defaults to read-only + AI-external, and full forces AI-external off', async () => {
    renderPage();
    const dialog = await openIssueDialog();

    const scope = within(dialog).getByLabelText(/Scope/);
    const aiExternal = within(dialog).getByLabelText('AI-external');
    expect(scope).toHaveValue('read_only');
    expect(aiExternal).toBeChecked();
    expect(aiExternal).toBeEnabled();

    fireEvent.change(scope, { target: { value: 'full' } });
    await waitFor(() => expect(within(dialog).getByLabelText('AI-external')).not.toBeChecked());
    expect(within(dialog).getByLabelText('AI-external')).toBeDisabled();
    expect(dialog).toHaveTextContent('Only a read-only token can be AI-external.');

    fireEvent.change(scope, { target: { value: 'read_only' } });
    await waitFor(() => expect(within(dialog).getByLabelText('AI-external')).toBeChecked());
  });

  it('offers only active, non-platform users', async () => {
    renderPage();
    const dialog = await openIssueDialog();
    await screen.findByText('Claude connector');

    const picker = within(dialog).getByLabelText(/Acts as user/);
    openComboBox(picker);
    const names = comboBoxOptions(picker).map(option => option.textContent);
    expect(names.some(name => name?.includes('Werco Assistant'))).toBe(true);
    expect(names.some(name => name?.includes('Olive Ops'))).toBe(true);
    // Inactive (409), platform admin and superuser (409) are never offered.
    expect(names.some(name => name?.includes('Gone Person'))).toBe(false);
    expect(names.some(name => name?.includes('Plat Form'))).toBe(false);
    expect(names.some(name => name?.includes('Root User'))).toBe(false);
  });

  it('issues a read-only AI-external token and reveals it exactly once', async () => {
    mockApi.createApiToken.mockResolvedValue(issued);
    const setItem = jest.spyOn(Storage.prototype, 'setItem');
    const consoleLog = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    renderPage();
    const dialog = await openIssueDialog();
    await screen.findByText('Claude connector');

    selectComboBoxOption(within(dialog).getByLabelText(/Acts as user/), /Werco Assistant/);
    fireEvent.change(within(dialog).getByLabelText(/Label/), { target: { value: '  New connector  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Issue token' }));

    await waitFor(() =>
      expect(mockApi.createApiToken).toHaveBeenCalledWith({
        user_id: 10,
        label: 'New connector',
        scope: 'read_only',
        ai_external: true,
      })
    );

    const reveal = await screen.findByTestId('api-token-reveal');
    expect(reveal).toHaveTextContent(/shown once/i);
    expect(reveal).toHaveTextContent(/acts as Werco Assistant/);
    expect(screen.getByTestId('api-token-value')).toHaveValue(SECRET);
    expect(screen.queryByRole('dialog', { name: /issue api token/i })).not.toBeInTheDocument();

    fireEvent.click(within(reveal).getByRole('button', { name: /copy token/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SECRET));
    expect(await within(reveal).findByRole('button', { name: 'Copied' })).toBeInTheDocument();

    // The list refreshes from the server after the write.
    expect(mockApi.listApiTokens).toHaveBeenCalledTimes(2);

    fireEvent.click(within(reveal).getByRole('button', { name: /done/i }));
    await waitFor(() => expect(screen.queryByTestId('api-token-reveal')).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(SECRET);

    // Never persisted, never logged.
    for (const call of setItem.mock.calls) {
      expect(String(call[1])).not.toContain(SECRET);
    }
    for (const call of consoleLog.mock.calls) {
      expect(call.map(String).join(' ')).not.toContain(SECRET);
    }
    setItem.mockRestore();
    consoleLog.mockRestore();
  });

  it('sends a full token with AI-external false and an expiry', async () => {
    mockApi.createApiToken.mockResolvedValue({ ...issued, scope: 'full', ai_external: false });
    renderPage();
    const dialog = await openIssueDialog();
    await screen.findByText('Claude connector');

    selectComboBoxOption(within(dialog).getByLabelText(/Acts as user/), /Olive Ops/);
    fireEvent.change(within(dialog).getByLabelText(/Label/), { target: { value: 'Ops bot' } });
    fireEvent.change(within(dialog).getByLabelText(/Scope/), { target: { value: 'full' } });
    fireEvent.change(within(dialog).getByLabelText(/Expires after/), { target: { value: '90' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Issue token' }));

    await waitFor(() =>
      expect(mockApi.createApiToken).toHaveBeenCalledWith({
        user_id: 11,
        label: 'Ops bot',
        scope: 'full',
        ai_external: false,
        expires_days: 90,
      })
    );
  });

  it('validates before calling the server', async () => {
    renderPage();
    const dialog = await openIssueDialog();
    await screen.findByText('Claude connector');

    fireEvent.change(within(dialog).getByLabelText(/Expires after/), { target: { value: '0' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Issue token' }));

    expect(await within(dialog).findByText('Choose the user this token acts as.')).toBeInTheDocument();
    expect(within(dialog).getByText('A label is required.')).toBeInTheDocument();
    expect(within(dialog).getByText(/Enter whole days from 1 to 3650/)).toBeInTheDocument();
    expect(mockApi.createApiToken).not.toHaveBeenCalled();
  });

  it('keeps the dialog open and toasts the server detail on a refusal', async () => {
    mockApi.createApiToken.mockRejectedValue({
      response: { status: 409, data: { detail: 'API tokens cannot be issued to an inactive user' } },
    });
    renderPage();
    const dialog = await openIssueDialog();
    await screen.findByText('Claude connector');

    selectComboBoxOption(within(dialog).getByLabelText(/Acts as user/), /Werco Assistant/);
    fireEvent.change(within(dialog).getByLabelText(/Label/), { target: { value: 'Connector' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Issue token' }));

    expect(await screen.findByText('API tokens cannot be issued to an inactive user')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: /issue api token/i })).toBeInTheDocument();
    expect(screen.queryByTestId('api-token-reveal')).not.toBeInTheDocument();
  });

  it('warns in the form when AI egress is off', async () => {
    mockApi.getCurrentCompany.mockResolvedValue({ id: 1, allow_ai_egress: false });
    renderPage();
    await screen.findByTestId('api-tokens-egress-off');
    const dialog = await openIssueDialog();
    expect(within(dialog).getByTestId('issue-egress-warning')).toHaveTextContent(/refused \(403\)/);
    fireEvent.change(within(dialog).getByLabelText(/Scope/), { target: { value: 'full' } });
    await waitFor(() => expect(within(dialog).queryByTestId('issue-egress-warning')).not.toBeInTheDocument());
  });
});

describe('API Tokens page — revoke', () => {
  it('revokes with a reason and refreshes from the server', async () => {
    mockApi.revokeApiToken.mockResolvedValue({ ...fullToken, revoked: true, revoke_reason: 'Bot retired' });
    renderPage();
    await screen.findByText('Grok bot');

    fireEvent.click(screen.getByRole('button', { name: 'Revoke Grok bot' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke API token' });
    expect(dialog).toHaveTextContent(/acts as Olive Ops/);
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: '  Bot retired ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));

    await waitFor(() => expect(mockApi.revokeApiToken).toHaveBeenCalledWith(2, 'Bot retired'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Revoke API token' })).not.toBeInTheDocument());
    expect(mockApi.listApiTokens).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/Revoked “Grok bot”/)).toBeInTheDocument();
  });

  it('refuses a too-short reason without calling the server', async () => {
    renderPage();
    await screen.findByText('Grok bot');
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Grok bot' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke API token' });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'no' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));

    expect(await screen.findByText(/at least 3 characters/)).toBeInTheDocument();
    expect(mockApi.revokeApiToken).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Revoke API token' })).toBeInTheDocument();
  });

  it('keeps the dialog open when the server refuses', async () => {
    mockApi.revokeApiToken.mockRejectedValue({
      response: { status: 409, data: { detail: 'API token is already revoked' } },
    });
    renderPage();
    await screen.findByText('Grok bot');
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Grok bot' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke API token' });
    fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Rotation' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }));

    expect(await screen.findByText('API token is already revoked')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Revoke API token' })).toBeInTheDocument();
    // Non-optimistic: the row still reads active.
    expect(within(tableRow('Grok bot')).getByText('active')).toBeInTheDocument();
  });
});
