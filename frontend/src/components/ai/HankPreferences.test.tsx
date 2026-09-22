import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import { DEFAULT_HANK_PREFERENCES, HankPreferencesResponse } from '../../types/hankPreferences';
import { formatCentralDateTime } from '../../utils/centralTime';
import { HankPreferences } from './HankPreferences';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankPreferences: jest.fn(),
    updateHankPreferences: jest.fn(),
    resetHankPreferences: jest.fn(),
  },
}));
const mockedApi = jest.mocked(api);
const defaults: HankPreferencesResponse = {
  company_id: 4,
  version: 0,
  preferences: DEFAULT_HANK_PREFERENCES,
  updated_at: null,
  can_edit: true,
};
const saved: HankPreferencesResponse = {
  ...defaults,
  version: 3,
  updated_at: '2026-09-22T13:30:00Z',
  preferences: {
    briefing_detail: 'concise',
    focus_area: 'quality',
    handoff_format: 'checklist',
    follow_up_alerts: false,
  },
};
function setSession(cid = 4, ro = false, sub = '17') {
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub, cid, ro, type: 'access' }))}.s`);
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}
function responseError(status: number, detail: string) {
  return { isAxiosError: true, response: { status, data: { detail } } };
}
function renderPreferences() {
  const onBusyChange = jest.fn();
  return { ...render(<HankPreferences onBusyChange={onBusyChange} />), onBusyChange };
}
async function changeDetail() {
  fireEvent.change(await screen.findByLabelText('Briefing detail'), { target: { value: 'concise' } });
}
function save() {
  fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }));
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  setSession();
  mockedApi.getHankPreferences.mockResolvedValue(defaults);
  mockedApi.updateHankPreferences.mockResolvedValue(saved);
  mockedApi.resetHankPreferences.mockResolvedValue({ ...defaults, version: 4, updated_at: '2026-09-22T13:35:00Z' });
});

describe('HankPreferences', () => {
  it('shows real unset defaults without writing and explains item caps and authorized focus ordering', async () => {
    renderPreferences();
    await screen.findByText('Using defaults. You have no saved preferences yet.');
    expect(screen.getByLabelText('Briefing detail')).toHaveValue('standard');
    expect(screen.getByLabelText('Show this area first')).toHaveValue('role_default');
    expect(screen.getByLabelText('Handoff format')).toHaveValue('bullets');
    expect(screen.getByLabelText('Follow-up alerts')).toBeChecked();
    expect(screen.getByText(/up to 3 items per section; standard shows up to 5/)).toBeInTheDocument();
    expect(screen.getByText(/while keeping other sections and your existing permissions/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Restore defaults' })).toBeDisabled();
    expect(mockedApi.updateHankPreferences).not.toHaveBeenCalled();
    expect(mockedApi.resetHankPreferences).not.toHaveBeenCalled();
  });

  it('recovers a load failure without assuming defaults', async () => {
    mockedApi.getHankPreferences.mockRejectedValueOnce(new Error('Offline'));
    renderPreferences();
    await screen.findByText('Your Hank preferences could not be loaded.');
    expect(screen.queryByLabelText('Briefing detail')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry preferences' }));
    await screen.findByText('Using defaults. You have no saved preferences yet.');
    expect(mockedApi.getHankPreferences).toHaveBeenCalledTimes(2);
  });

  it('saves all four choices with the current company/version only on explicit submission', async () => {
    const pending = deferred<HankPreferencesResponse>();
    mockedApi.updateHankPreferences.mockReturnValue(pending.promise);
    const { onBusyChange } = renderPreferences();
    await changeDetail();
    fireEvent.change(screen.getByLabelText('Show this area first'), { target: { value: 'quality' } });
    fireEvent.change(screen.getByLabelText('Handoff format'), { target: { value: 'checklist' } });
    fireEvent.click(screen.getByLabelText('Follow-up alerts'));
    expect(mockedApi.updateHankPreferences).not.toHaveBeenCalled();
    save();
    await waitFor(() =>
      expect(mockedApi.updateHankPreferences).toHaveBeenCalledWith(
        { expected_company_id: 4, expected_version: 0, preferences: saved.preferences },
        expect.any(AbortSignal)
      )
    );
    expect(screen.getByLabelText('Briefing detail')).toBeDisabled();
    expect(screen.queryByText('Preferences saved.')).not.toBeInTheDocument();
    expect(onBusyChange).toHaveBeenCalledWith(true);
    await act(async () => pending.resolve(saved));
    expect(screen.getByText('Preferences saved.')).toBeInTheDocument();
    expect(screen.getByText(`Last saved ${formatCentralDateTime(saved.updated_at!)}`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled();
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it('loads saved values and restores defaults only after the server confirms', async () => {
    mockedApi.getHankPreferences.mockResolvedValue(saved);
    const pending = deferred<HankPreferencesResponse>();
    mockedApi.resetHankPreferences.mockReturnValue(pending.promise);
    renderPreferences();
    expect(await screen.findByLabelText('Briefing detail')).toHaveValue('concise');
    expect(screen.getByLabelText('Follow-up alerts')).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Restore defaults' }));
    expect(mockedApi.resetHankPreferences).toHaveBeenCalledWith(
      { expected_company_id: 4, expected_version: 3 },
      expect.any(AbortSignal)
    );
    expect(screen.getByLabelText('Briefing detail')).toHaveValue('concise');
    expect(screen.queryByText('Default preferences restored.')).not.toBeInTheDocument();
    await act(async () => pending.resolve({ ...defaults, version: 4 }));
    expect(screen.getByText('Default preferences restored.')).toBeInTheDocument();
    expect(screen.getByLabelText('Briefing detail')).toHaveValue('standard');
    expect(screen.getByLabelText('Follow-up alerts')).toBeChecked();
  });

  it('uses server edit capability and read-only token claims to prevent saves', async () => {
    mockedApi.getHankPreferences.mockResolvedValue({ ...saved, can_edit: false });
    const first = renderPreferences();
    await screen.findByLabelText('Briefing detail');
    expect(screen.getByLabelText('Briefing detail')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save preferences' })).not.toBeInTheDocument();
    first.unmount();
    setSession(4, true);
    mockedApi.getHankPreferences.mockResolvedValue(saved);
    renderPreferences();
    await screen.findByLabelText('Briefing detail');
    expect(screen.getByLabelText('Follow-up alerts')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Restore defaults' })).not.toBeInTheDocument();
    expect(mockedApi.updateHankPreferences).not.toHaveBeenCalled();
  });

  it('recovers a lost save response through a read and never writes twice', async () => {
    mockedApi.updateHankPreferences.mockRejectedValueOnce(new Error('Response lost'));
    renderPreferences();
    await changeDetail();
    save();
    await screen.findByText(/Reload saved preferences before changing them again/);
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Restore defaults' })).toBeDisabled();
    mockedApi.getHankPreferences.mockResolvedValue(saved);
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved preferences' }));
    await screen.findByText('Saved preferences loaded. Review them before making further changes.');
    expect(screen.getByLabelText('Show this area first')).toHaveValue('quality');
    expect(screen.getByLabelText('Follow-up alerts')).not.toBeChecked();
    expect(mockedApi.updateHankPreferences).toHaveBeenCalledTimes(1);
  });

  it('holds stale selections until explicit reload and uses the refreshed version for the next save', async () => {
    mockedApi.updateHankPreferences.mockRejectedValueOnce(responseError(409, 'Preferences changed elsewhere.'));
    renderPreferences();
    await changeDetail();
    save();
    await screen.findByText('Preferences changed elsewhere.');
    expect(screen.getByLabelText('Briefing detail')).toHaveValue('concise');
    expect(screen.getByLabelText('Briefing detail')).toBeDisabled();
    mockedApi.getHankPreferences.mockResolvedValue({ ...defaults, version: 7 });
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved preferences' }));
    await screen.findByText('Saved preferences loaded. Review them before making further changes.');
    expect(screen.getByLabelText('Briefing detail')).toHaveValue('standard');
    fireEvent.change(screen.getByLabelText('Handoff format'), { target: { value: 'checklist' } });
    save();
    await screen.findByText('Preferences saved.');
    expect(mockedApi.updateHankPreferences).toHaveBeenLastCalledWith(
      {
        expected_company_id: 4,
        expected_version: 7,
        preferences: { ...DEFAULT_HANK_PREFERENCES, handoff_format: 'checklist' },
      },
      expect.any(AbortSignal)
    );
  });

  it('keeps writes blocked when recovery fails and allows retrying the read', async () => {
    mockedApi.resetHankPreferences.mockRejectedValue(new Error('Lost response'));
    mockedApi.getHankPreferences
      .mockResolvedValueOnce(saved)
      .mockRejectedValueOnce(new Error('Offline'))
      .mockResolvedValue({ ...defaults, version: 4 });
    renderPreferences();
    fireEvent.click(await screen.findByRole('button', { name: 'Restore defaults' }));
    await screen.findByText(/Reload saved preferences before changing them again/);
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved preferences' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reload saved preferences' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Restore defaults' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved preferences' }));
    await screen.findByText('Saved preferences loaded. Review them before making further changes.');
    expect(screen.getByLabelText('Follow-up alerts')).toBeChecked();
    expect(mockedApi.resetHankPreferences).toHaveBeenCalledTimes(1);
  });

  it('keeps editing available after an explicit validation refusal', async () => {
    mockedApi.updateHankPreferences.mockRejectedValueOnce(responseError(422, 'Unsupported preference.'));
    renderPreferences();
    await changeDetail();
    save();
    await screen.findByText('Unsupported preference.');
    expect(screen.getByLabelText('Briefing detail')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeEnabled();
  });

  it.each(['company', 'user'] as const)(
    'aborts a save synchronously when the %s changes and hides old settings',
    async identity => {
      const pending = deferred<HankPreferencesResponse>();
      mockedApi.updateHankPreferences.mockReturnValue(pending.promise);
      renderPreferences();
      await changeDetail();
      save();
      await waitFor(() => expect(mockedApi.updateHankPreferences).toHaveBeenCalledTimes(1));
      const signal = mockedApi.updateHankPreferences.mock.calls[0][1];
      setSession(identity === 'company' ? 5 : 4, false, identity === 'user' ? '18' : '17');
      act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
      expect(signal?.aborted).toBe(true);
      expect(screen.getByRole('alert')).toHaveTextContent('Your session changed');
      expect(screen.queryByLabelText('Briefing detail')).not.toBeInTheDocument();
      await act(async () => pending.resolve(saved));
      expect(screen.queryByText('Preferences saved.')).not.toBeInTheDocument();
    }
  );

  it('aborts an initial preference read on unmount and ignores a late response', async () => {
    const pending = deferred<HankPreferencesResponse>();
    mockedApi.getHankPreferences.mockReturnValue(pending.promise);
    const { unmount, onBusyChange } = renderPreferences();
    const signal = mockedApi.getHankPreferences.mock.calls[0][0];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(saved));
    expect(onBusyChange).not.toHaveBeenCalledWith(true);
  });

  it('aborts an in-flight reset on unmount', async () => {
    mockedApi.getHankPreferences.mockResolvedValue(saved);
    const pending = deferred<HankPreferencesResponse>();
    mockedApi.resetHankPreferences.mockReturnValue(pending.promise);
    const { unmount, onBusyChange } = renderPreferences();
    fireEvent.click(await screen.findByRole('button', { name: 'Restore defaults' }));
    const signal = mockedApi.resetHankPreferences.mock.calls[0][1];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(defaults));
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
  });
});
