/**
 * Register — "email OR employee ID (plus password)".
 *
 * The page used to mark Email `required` and describe Employee ID as
 * "auto-generated if left blank", so the browser itself enforced an email on every
 * signup. Both are now individually optional and the form owns the at-least-one rule,
 * because there is nothing left in the markup to enforce it — `required` was the
 * enforcement, and removing it without replacing it would have sent `{}` identifiers
 * to the server and turned a fixable typo into a 422 round trip.
 *
 * What these four tests pin, and why each one is a separate claim:
 *
 * 1. The guard REFUSES rather than just warns — `registerPublic` must not be called at
 *    all. A version that showed the message and posted anyway would look identical on
 *    screen and still burn one of the route's 3-per-minute attempts.
 * 2. and 3. The payload OMITS the empty identifier's key rather than sending `''`.
 *    That distinction is the whole contract: the backend's `PublicRegister` validator
 *    accepts a missing `email` and mints an address, but an empty-string `email` fails
 *    `EmailStr` and an empty `employee_id` fails its field pattern — both 422, and
 *    neither reaches the "either one is enough" rule. So the assertions are written
 *    against the exact object (`toHaveBeenCalledWith` + an explicit key check), not
 *    `objectContaining`, which would pass on a payload carrying `email: ''`.
 * 4. The rule that replaced `required` is announced to assistive tech, not merely
 *    printed — see the comment on that test.
 *
 * services/api is mocked at the module boundary; no network. `getSetupStatus` is
 * stubbed because the page calls it on mount to decide whether to show the first-user
 * setup banner — unmocked it would reject and (harmlessly) log.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Register from './Register';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    registerPublic: jest.fn(),
    getSetupStatus: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const PASSWORD = 'CorrectHorseBatteryStaple';

/** Fill everything the form needs EXCEPT the identifiers, then submit. */
const fillAndSubmit = (identifiers: { email?: string; employeeId?: string }) => {
  render(
    <MemoryRouter>
      <Register />
    </MemoryRouter>
  );

  fireEvent.change(screen.getByLabelText('First Name'), { target: { value: 'Ada' } });
  fireEvent.change(screen.getByLabelText('Last Name'), { target: { value: 'Lovelace' } });
  if (identifiers.email !== undefined) {
    fireEvent.change(screen.getByLabelText('Email Address'), { target: { value: identifiers.email } });
  }
  if (identifiers.employeeId !== undefined) {
    fireEvent.change(screen.getByLabelText('Employee ID'), { target: { value: identifiers.employeeId } });
  }
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: PASSWORD } });
  fireEvent.change(screen.getByLabelText('Confirm Password'), { target: { value: PASSWORD } });

  fireEvent.click(screen.getByRole('button', { name: /create account/i }));
};

describe('Register — email or employee ID', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getSetupStatus.mockResolvedValue({ has_users: true, is_setup_required: false });
    mockedApi.registerPublic.mockResolvedValue({ message: 'Account submitted for approval', is_first_user: false });
  });

  /**
   * 4. The rule is REACHABLE to a screen reader, not just visible.
   *
   * Removing `required` from Email took the browser's own enforcement away, and
   * dropping "(optional)" from the Employee ID label took away the only other hint that
   * either field could be left blank. What replaced both is one sentence of prose — and
   * prose sitting loose in the form is announced only if the user happens to arrow
   * through it, which nobody does while tabbing between inputs. So it is wired to BOTH
   * inputs by `aria-describedby`, and that wiring is the thing asserted: the text
   * rendering somewhere on the page is not the same claim.
   *
   * `toHaveAccessibleDescription` is used rather than reading the `aria-describedby`
   * attribute because it RESOLVES the id — an attribute check passes just as happily
   * when it points at an element that no longer exists, which is exactly what a later
   * refactor of this paragraph would produce.
   */
  it('describes the either-identifier rule to both identifier inputs', async () => {
    render(
      <MemoryRouter>
        <Register />
      </MemoryRouter>
    );
    await waitFor(() => expect(mockedApi.getSetupStatus).toHaveBeenCalled());

    const rule = /email, your employee ID, or both/;
    expect(screen.getByLabelText('Email Address')).toHaveAccessibleDescription(rule);
    expect(screen.getByLabelText('Employee ID')).toHaveAccessibleDescription(rule);
  });

  it('refuses to submit when neither identifier is filled in', async () => {
    fillAndSubmit({});

    expect(await screen.findByText('Enter an email address or an employee ID.')).toBeInTheDocument();
    expect(mockedApi.registerPublic).not.toHaveBeenCalled();
  });

  it('submits an employee ID with no email key at all', async () => {
    fillAndSubmit({ employeeId: 'EMP-0777' });

    await waitFor(() => expect(mockedApi.registerPublic).toHaveBeenCalledTimes(1));
    expect(mockedApi.registerPublic).toHaveBeenCalledWith({
      first_name: 'Ada',
      last_name: 'Lovelace',
      employee_id: 'EMP-0777',
      password: PASSWORD,
    });
    // Explicit: absent, not present-and-empty. The backend treats those differently.
    expect('email' in mockedApi.registerPublic.mock.calls[0][0]).toBe(false);
  });

  it('submits an email with no employee_id key at all', async () => {
    fillAndSubmit({ email: 'ada@wercomfg.com' });

    await waitFor(() => expect(mockedApi.registerPublic).toHaveBeenCalledTimes(1));
    expect(mockedApi.registerPublic).toHaveBeenCalledWith({
      first_name: 'Ada',
      last_name: 'Lovelace',
      email: 'ada@wercomfg.com',
      password: PASSWORD,
    });
    expect('employee_id' in mockedApi.registerPublic.mock.calls[0][0]).toBe(false);
  });
});
