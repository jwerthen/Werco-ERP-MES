/**
 * Self-service password change — `POST /api/v1/users/change-password`
 * (backend/app/api/endpoints/users.py → `PasswordChange` / `change_own_password`).
 *
 * People-only: the server refuses any API token, kiosk badge token or station token
 * with a 403 before it reads the passwords. 400 = wrong current password, 422 = the
 * new password fails the policy, 503 = the required audit row could not be saved
 * (nothing was changed).
 */
export interface ChangeOwnPasswordRequest {
  current_password: string;
  new_password: string;
}

export interface ChangeOwnPasswordResponse {
  message: string;
}
