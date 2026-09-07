/**
 * Role-based default landing after login (B0.3 "Action Inbox as the front door").
 *
 * Managerial roles land on the Action Inbox so the system tells them what needs
 * attention; operators keep the kiosk-mode shop-floor screen. Everyone else keeps
 * the classic dashboard at "/". Only the post-login DEFAULT is decided here —
 * deep links and in-app navigation are untouched.
 */
export const DEFAULT_LANDING_BY_ROLE: Record<string, string> = {
  operator: '/shop-floor/operations?kiosk=1',
  admin: '/action-inbox',
  manager: '/action-inbox',
  supervisor: '/action-inbox',
};

export function getDefaultLandingPath(role?: string | null): string {
  if (!role) return '/';
  return DEFAULT_LANDING_BY_ROLE[role] ?? '/';
}

/** Accept local application destinations only, preserving their query and fragment. */
export function safeReturnPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || /[\\\r\n]/.test(value))
    return null;
  const path = value.split(/[?#]/)[0];
  return ['/login', '/register', '/company-register'].includes(path) ? null : value;
}
