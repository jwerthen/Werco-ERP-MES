/**
 * SmsSettingsSection — the self-service SMS slice of My Settings.
 *
 * Covers the three states that would otherwise make an opt-in silently do nothing:
 * NO PHONE SAVED (toggles disabled + hint, test send disabled), COMPANY SMS EGRESS
 * OFF (warning banner naming Admin Settings, test send disabled), and PROVIDER NOT
 * CONFIGURED. Plus: the event list is driven entirely by the catalog's
 * `sms_eligible` flag (never hardcoded), an opt-in merge-saves only that event's
 * `sms` key, a rejected save rolls the checkbox back and surfaces the server's
 * verbatim detail, the phone save normalizes to E.164 and rejects garbage
 * client-side, clearing the number sends null, and a `status: "skipped"` test send
 * is reported as a failure rather than a false success.
 *
 * Fixtures mirror the backend contracts in `backend/app/schemas/notification.py`
 * (`NotificationPreferencesResponse`, `TestSMSResponse`). services/api is mocked at
 * the module boundary — no real network, and no provider credential appears
 * anywhere in the fixtures.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SmsSettingsSection from './SmsSettingsSection';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { NotificationCatalogEntry, NotificationPreferences } from '../../types/notification';
import type { User } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNotificationCatalog: jest.fn(),
    getMyNotificationPreferences: jest.fn(),
    updateMyNotificationPreferences: jest.fn(),
    updateMyPhone: jest.fn(),
    sendTestSms: jest.fn(),
  },
}));

// The component reads the current user only to decide whether to LINK the (permission-gated)
// admin settings page or just tell the user to ask an admin. Mocked at the module boundary,
// matching SmsEgressTab.test.tsx. Defaults to a non-admin operator; the admin case is asserted
// explicitly below.
jest.mock('../../context/AuthContext', () => ({
  __esModule: true,
  useAuth: jest.fn(),
}));

const mockApi = api as jest.Mocked<typeof api>;
const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

const setAuthRole = (role: string) =>
  mockUseAuth.mockReturnValue({ user: { id: 7, role, is_superuser: false } } as ReturnType<typeof useAuth>);

const httpError = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const user = (phone: string | null = null): User =>
  ({
    id: 7,
    version: 1,
    employee_id: '0007',
    email: 'operator@acme.com',
    first_name: 'Otto',
    last_name: 'Operator',
    role: 'operator',
    phone,
    is_active: true,
    is_superuser: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }) as User;

const entry = (over: Partial<NotificationCatalogEntry>): NotificationCatalogEntry => ({
  event_key: 'wo.blocker_created',
  label: 'Work order blocked',
  description: 'A work order was placed on hold by a blocker.',
  category: 'production',
  severity: 'critical',
  default_channels: ['in_app', 'email'],
  mandatory_channel: null,
  sms_eligible: true,
  ...over,
});

const CATALOG: NotificationCatalogEntry[] = [
  entry({}),
  entry({
    event_key: 'ncr.created',
    label: 'NCR raised',
    description: 'A nonconformance report was created.',
    category: 'quality',
  }),
  // Not SMS-eligible — must never render a toggle here.
  entry({
    event_key: 'po.received',
    label: 'PO received',
    description: 'A purchase order receipt was posted.',
    category: 'purchasing',
    severity: 'info',
    sms_eligible: false,
  }),
];

const renderSection = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <SmsSettingsSection />
      </ToastProvider>
    </MemoryRouter>,
  );

/** Mirrors `NotificationPreferencesResponse` from the backend. */
const prefsResponse = ({
  phone = null,
  allowSms = true,
  configured = true,
  preferences = {},
}: {
  phone?: string | null;
  allowSms?: boolean;
  configured?: boolean;
  preferences?: Record<string, Record<string, boolean>>;
} = {}): NotificationPreferences => ({
  preferences,
  has_saved_preferences: Object.keys(preferences).length > 0,
  phone,
  sms_egress_enabled: allowSms,
  sms_configured: configured,
});

const setup = ({
  catalog = CATALOG,
  ...prefs
}: {
  phone?: string | null;
  allowSms?: boolean;
  configured?: boolean;
  preferences?: Record<string, Record<string, boolean>>;
  catalog?: NotificationCatalogEntry[];
} = {}) => {
  mockApi.getNotificationCatalog.mockResolvedValue(catalog);
  mockApi.getMyNotificationPreferences.mockResolvedValue(prefsResponse(prefs));
};

const toggleFor = (label: RegExp) => screen.getByRole('checkbox', { name: label });

beforeEach(() => {
  jest.clearAllMocks();
  setAuthRole('operator');
  setup();
});

describe('SmsSettingsSection — event list', () => {
  it('renders one toggle per sms_eligible catalog entry and nothing for the rest', async () => {
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(screen.getByText(/ncr raised/i)).toBeInTheDocument();
    // `po.received` is not sms_eligible — it must not appear.
    expect(screen.queryByText(/po received/i)).toBeNull();
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);
  });

  it('shows an empty state when no catalog event is SMS-eligible', async () => {
    setup({ catalog: [entry({ sms_eligible: false })] });
    renderSection();

    await waitFor(() => expect(screen.getByText(/no events offer text alerts/i)).toBeInTheDocument());
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('surfaces a retryable error state when the load fails', async () => {
    mockApi.getNotificationCatalog.mockRejectedValueOnce(httpError(500, 'boom'));
    renderSection();

    await waitFor(() => expect(screen.getByTestId('error-state')).toBeInTheDocument());
    setup();
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
  });
});

describe('SmsSettingsSection — no phone saved', () => {
  it('disables every SMS toggle and explains why', async () => {
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(toggleFor(/work order blocked/i)).toBeDisabled();
    expect(toggleFor(/ncr raised/i)).toBeDisabled();
    expect(screen.getByText(/save a mobile number above to turn any of these on/i)).toBeInTheDocument();
    expect(screen.getByText(/no mobile number saved/i)).toBeInTheDocument();
  });

  it('disables the test send until a number is saved', async () => {
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    const testButton = screen.getByRole('button', { name: /send test sms/i });
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveAttribute('title', 'Save a mobile number first');
    fireEvent.click(testButton);
    expect(mockApi.sendTestSms).not.toHaveBeenCalled();
  });
});

describe('SmsSettingsSection — company egress off', () => {
  it('warns that SMS is off company-wide and points an ADMIN at Admin Settings', async () => {
    setAuthRole('admin');
    setup({ phone: '+15125550142', allowSms: false });
    renderSection();

    await waitFor(() =>
      expect(screen.getByText(/text messages are switched off company-wide/i)).toBeInTheDocument(),
    );
    const link = screen.getByRole('link', { name: /admin settings → sms privacy/i });
    expect(link).toHaveAttribute('href', '/admin/settings?tab=smsprivacy');
  });

  it('does NOT link a non-admin to the permission-gated admin page, and says who to ask', async () => {
    // /admin/settings is permission-gated; linking every operator there just dead-ends them.
    setAuthRole('operator');
    setup({ phone: '+15125550142', allowSms: false });
    renderSection();

    await waitFor(() =>
      expect(screen.getByText(/text messages are switched off company-wide/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole('link', { name: /admin settings → sms privacy/i })).not.toBeInTheDocument();
    expect(screen.getByText(/ask an administrator to enable sms egress/i)).toBeInTheDocument();
  });

  it('disables the test send while egress is off, even with a saved number', async () => {
    setup({ phone: '+15125550142', allowSms: false });
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    const testButton = screen.getByRole('button', { name: /send test sms/i });
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveAttribute('title', 'SMS is switched off company-wide');
  });

  it('still lets a user pre-set their opt-ins (preferences are not blocked by egress)', async () => {
    setup({ phone: '+15125550142', allowSms: false });
    mockApi.updateMyNotificationPreferences.mockResolvedValue(
      prefsResponse({
        phone: '+15125550142',
        allowSms: false,
        preferences: { 'wo.blocker_created': { sms: true } },
      }),
    );
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    const toggle = toggleFor(/work order blocked/i);
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(mockApi.updateMyNotificationPreferences).toHaveBeenCalledWith({
        preferences: { 'wo.blocker_created': { sms: true } },
      }),
    );
  });

  it('shows no company-wide warning when egress is on', async () => {
    setup({ phone: '+15125550142', allowSms: true });
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(screen.queryByText(/switched off company-wide/i)).toBeNull();
  });
});

describe('SmsSettingsSection — opt-in toggles', () => {
  beforeEach(() => setup({ phone: '+15125550142', allowSms: true }));

  it('defaults to off when the catalog default omits sms and no preference is stored', async () => {
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(toggleFor(/work order blocked/i)).not.toBeChecked();
  });

  it('reflects a stored preference over the catalog default', async () => {
    setup({
      phone: '+15125550142',
      preferences: { 'wo.blocker_created': { sms: true }, 'ncr.created': { sms: false } },
    });
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(toggleFor(/work order blocked/i)).toBeChecked();
    expect(toggleFor(/ncr raised/i)).not.toBeChecked();
  });

  it('merge-saves ONLY the sms key for the toggled event', async () => {
    mockApi.updateMyNotificationPreferences.mockResolvedValue(
      prefsResponse({ phone: '+15125550142', preferences: { 'ncr.created': { sms: true } } }),
    );
    renderSection();

    await waitFor(() => expect(screen.getByText(/ncr raised/i)).toBeInTheDocument());
    fireEvent.click(toggleFor(/ncr raised/i));

    await waitFor(() =>
      expect(mockApi.updateMyNotificationPreferences).toHaveBeenCalledWith({
        preferences: { 'ncr.created': { sms: true } },
      }),
    );
    // The untouched event is not part of the payload.
    const payload = mockApi.updateMyNotificationPreferences.mock.calls[0][0];
    expect(Object.keys(payload.preferences ?? {})).toEqual(['ncr.created']);
  });

  it('rolls the checkbox back and shows the server detail when the save is rejected', async () => {
    mockApi.updateMyNotificationPreferences.mockRejectedValueOnce(
      httpError(400, 'This event is not eligible for SMS'),
    );
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    fireEvent.click(toggleFor(/work order blocked/i));

    await waitFor(() => expect(mockApi.updateMyNotificationPreferences).toHaveBeenCalled());
    await waitFor(() => expect(toggleFor(/work order blocked/i)).not.toBeChecked());
    expect(await screen.findByText('This event is not eligible for SMS')).toBeInTheDocument();
  });

  it('forces a mandatory-SMS event on and locks the toggle', async () => {
    setup({
      phone: '+15125550142',
      catalog: [entry({ mandatory_channel: 'sms', default_channels: ['in_app', 'sms'] })],
    });
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    const toggle = toggleFor(/work order blocked/i);
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/always on/i)).toBeInTheDocument();
  });
});

describe('SmsSettingsSection — phone number', () => {
  it('normalizes a typed US number to E.164 before saving', async () => {
    renderSection();
    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());

    mockApi.updateMyPhone.mockResolvedValueOnce(user('+15125550142'));
    fireEvent.change(screen.getByLabelText(/mobile number for text alerts/i), {
      target: { value: '(512) 555-0142' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save number/i }));

    await waitFor(() => expect(mockApi.updateMyPhone).toHaveBeenCalledWith('+15125550142'));
    // The saved number is echoed back and the toggles come alive.
    await waitFor(() => expect(toggleFor(/work order blocked/i)).not.toBeDisabled());
    expect(screen.getByText(/\+1 \(512\) 555-0142/)).toBeInTheDocument();
  });

  it('rejects an implausible number client-side without calling the API', async () => {
    renderSection();
    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/mobile number for text alerts/i), {
      target: { value: '12345' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save number/i }));

    expect(await screen.findByText(/enter a mobile number with its country code/i)).toBeInTheDocument();
    expect(mockApi.updateMyPhone).not.toHaveBeenCalled();
  });

  it('surfaces the server 422 detail verbatim when normalization is refused', async () => {
    renderSection();
    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());

    mockApi.updateMyPhone.mockRejectedValueOnce(httpError(422, 'Not a valid mobile number'));
    fireEvent.change(screen.getByLabelText(/mobile number for text alerts/i), {
      target: { value: '+15125550142' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save number/i }));

    expect(await screen.findByText('Not a valid mobile number')).toBeInTheDocument();
  });

  it('sends null to clear the number and re-disables the toggles', async () => {
    setup({ phone: '+15125550142' });
    renderSection();
    await waitFor(() => expect(toggleFor(/work order blocked/i)).not.toBeDisabled());

    mockApi.updateMyPhone.mockResolvedValueOnce(user(null));
    fireEvent.click(screen.getByRole('button', { name: /remove number/i }));

    await waitFor(() => expect(mockApi.updateMyPhone).toHaveBeenCalledWith(null));
    await waitFor(() => expect(toggleFor(/work order blocked/i)).toBeDisabled());
    expect(screen.getByText(/no mobile number saved/i)).toBeInTheDocument();
  });

  it('offers no Remove button when there is nothing to remove', async () => {
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /remove number/i })).toBeNull();
  });
});

describe('SmsSettingsSection — provider not configured', () => {
  it('says setup is unfinished and blocks the test send', async () => {
    setup({ phone: '+15125550142', allowSms: true, configured: false });
    renderSection();

    await waitFor(() =>
      expect(screen.getByText(/text messaging is not finished being set up/i)).toBeInTheDocument(),
    );
    const testButton = screen.getByRole('button', { name: /send test sms/i });
    expect(testButton).toBeDisabled();
    expect(testButton).toHaveAttribute('title', 'Text messaging is not set up on this server yet');
  });

  it('shows no setup warning when the provider is configured', async () => {
    setup({ phone: '+15125550142', allowSms: true, configured: true });
    renderSection();

    await waitFor(() => expect(screen.getByText(/work order blocked/i)).toBeInTheDocument());
    expect(screen.queryByText(/not finished being set up/i)).toBeNull();
  });
});

describe('SmsSettingsSection — test send', () => {
  beforeEach(() => setup({ phone: '+15125550142', allowSms: true }));

  it('posts the test send and reports the server detail', async () => {
    mockApi.sendTestSms.mockResolvedValueOnce({ status: 'sent', sid: 'SM123', detail: 'Test message sent.' });
    renderSection();

    await waitFor(() => expect(screen.getByRole('button', { name: /send test sms/i })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: /send test sms/i }));

    await waitFor(() => expect(mockApi.sendTestSms).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Test message sent.')).toBeInTheDocument();
    // The provider message id is bookkeeping — it must never reach the UI.
    expect(screen.queryByText(/SM123/)).toBeNull();
  });

  it('treats a 200 with status "skipped" as a failure, not a success', async () => {
    mockApi.sendTestSms.mockResolvedValueOnce({
      status: 'skipped',
      detail: 'SMS is not configured on this server. Ask an administrator to finish Twilio setup.',
    });
    renderSection();

    await waitFor(() => expect(screen.getByRole('button', { name: /send test sms/i })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: /send test sms/i }));

    expect(await screen.findByText(/ask an administrator to finish twilio setup/i)).toBeInTheDocument();
    // …and the UI now reflects the unconfigured provider.
    await waitFor(() =>
      expect(screen.getByText(/text messaging is not finished being set up/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /send test sms/i })).toBeDisabled();
  });

  it('reports a refused test send with the server detail (fail-closed paths)', async () => {
    mockApi.sendTestSms.mockRejectedValueOnce(
      httpError(400, 'SMS is turned off for this company. An admin can enable it in Admin Settings.'),
    );
    renderSection();

    await waitFor(() => expect(screen.getByRole('button', { name: /send test sms/i })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: /send test sms/i }));

    expect(
      await screen.findByText(/sms is turned off for this company\. an admin can enable it/i),
    ).toBeInTheDocument();
  });
});

describe('SmsSettingsSection — CUI content rule', () => {
  it('tells the user SMS bodies are terse and carry no record detail', async () => {
    renderSection();

    const note = await screen.findByText(/text alerts are opt-in/i);
    expect(within(note).getByText(/never customer names, part details, or quantities/i)).toBeInTheDocument();
  });
});
