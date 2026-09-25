import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider, useAuth } from './AuthContext';
import { getShopFloorIdleTimeoutMs, usePersonalShopFloorSession } from '../hooks/usePersonalShopFloorSession';
import api from '../services/api';

jest.mock('../services/api', () => ({ __esModule: true, default: { getCurrentUser: jest.fn(), logout: jest.fn() } }));
const operator = {
  id: 3,
  company_id: 1,
  email: 'operator@example.test',
  role: 'operator',
  employee_id: 'E3',
  first_name: 'A',
  last_name: 'Worker',
  is_active: true,
};
const preferenceKey = 'shop_floor_personal_phone:v1:company:1:user:3';
let phoneSize = true;

function PhoneSettings() {
  const auth = useAuth();
  const session = usePersonalShopFloorSession(auth.user);
  return (
    <>
      <span>{auth.sessionWarning ? 'warning' : 'active'}</span>
      <span>{session.available ? 'personal option available' : 'shared or desktop'}</span>
      <span>{session.timeoutMinutes} minutes</span>
      <button onClick={() => session.setPersonalPhone(!session.personalPhone)}>Personal phone</button>
    </>
  );
}

async function mount(path = '/shop-floor/operations?kiosk=1') {
  window.history.replaceState({}, '', path);
  await act(async () => {
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={[path]}>
          <PhoneSettings />
        </MemoryRouter>
      </AuthProvider>
    );
  });
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  phoneSize = true;
  window.matchMedia = jest.fn(() => ({
    matches: phoneSize,
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
  })) as unknown as typeof window.matchMedia;
  localStorage.clear();
  sessionStorage.clear();
  sessionStorage.setItem('token', 'token');
  sessionStorage.setItem('user', JSON.stringify(operator));
  (api.getCurrentUser as jest.Mock).mockResolvedValue(operator);
});
afterEach(() => {
  jest.useRealTimers();
  window.history.replaceState({}, '', '/');
});

it('keeps 15 minutes by default even on a personal-size phone', async () => {
  await mount();
  expect(screen.getByText('personal option available')).toBeInTheDocument();
  expect(screen.getByText('15 minutes')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(14 * 60_000));
  expect(screen.getByText('warning')).toBeInTheDocument();
});

it('uses 30 minutes only after explicit opt-in, including simplified badge navigation', async () => {
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Personal phone' }));
  expect(screen.getByText('30 minutes')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(15 * 60_000));
  expect(api.logout).not.toHaveBeenCalled();
  expect(screen.getByText('active')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(14 * 60_000));
  expect(screen.getByText('warning')).toBeInTheDocument();
  act(() => jest.advanceTimersByTime(60_000));
  expect(api.logout).toHaveBeenCalledTimes(1);
});

it.each(['/kiosk', '/kiosk?station=2', '/shop-floor/operations?station=2', '/work-orders'])(
  'cannot apply the saved phone preference on %s',
  async path => {
    localStorage.setItem(preferenceKey, '1');
    await mount(path);
    expect(screen.getByText('shared or desktop')).toBeInTheDocument();
    expect(getShopFloorIdleTimeoutMs(operator)).toBe(15 * 60_000);
    fireEvent.click(screen.getByRole('button', { name: 'Personal phone' }));
    expect(screen.getByText('15 minutes')).toBeInTheDocument();
  }
);

it('never uses the phone preference on a desktop or for a different operator', async () => {
  localStorage.setItem(preferenceKey, '1');
  phoneSize = false;
  await mount();
  expect(getShopFloorIdleTimeoutMs(operator)).toBe(15 * 60_000);
  expect(getShopFloorIdleTimeoutMs({ id: 4, company_id: 1 })).toBe(15 * 60_000);
});

it('opting out restores the default and leaving shop floor cannot retain an extended session', async () => {
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Personal phone' }));
  expect(getShopFloorIdleTimeoutMs(operator)).toBe(30 * 60_000);
  window.history.replaceState({}, '', '/work-orders');
  expect(getShopFloorIdleTimeoutMs(operator)).toBe(15 * 60_000);
  window.history.replaceState({}, '', '/shop-floor/operations');
  fireEvent.click(screen.getByRole('button', { name: 'Personal phone' }));
  expect(screen.getByText('15 minutes')).toBeInTheDocument();
  expect(localStorage.getItem(preferenceKey)).toBeNull();
});
