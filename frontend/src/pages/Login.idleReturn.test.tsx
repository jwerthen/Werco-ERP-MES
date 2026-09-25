import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Login from './Login';

const mockNavigate = jest.fn();
const mockBadgeLogin = jest.fn();
jest.mock('react-router-dom', () => ({ ...jest.requireActual('react-router-dom'), useNavigate: () => mockNavigate }));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ login: jest.fn(), loginWithEmployeeId: mockBadgeLogin }),
}));

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  mockBadgeLogin.mockImplementation(async () =>
    sessionStorage.setItem('user', JSON.stringify({ id: 3, company_id: 1, role: 'operator' }))
  );
});

it('returns badge operators to their requested job screen without switching back to password mode', async () => {
  const destination = '/shop-floor/operations?kiosk=1&work_center_id=8#operation-82';
  render(
    <MemoryRouter initialEntries={[`/login?reason=idle&mode=employee&returnTo=${encodeURIComponent(destination)}`]}>
      <Login />
    </MemoryRouter>
  );
  expect(screen.getByRole('status')).toHaveTextContent('This did not check you out of your job');
  fireEvent.change(screen.getByLabelText('Employee / Badge ID'), { target: { value: '23' } });
  fireEvent.click(screen.getByRole('button', { name: /sign in/i }));
  await waitFor(() => expect(mockBadgeLogin).toHaveBeenCalledWith('0023'));
  await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith(destination, { replace: true }));
});
