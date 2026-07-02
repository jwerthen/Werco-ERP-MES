const KIOSK_STORAGE_KEY = 'kiosk_mode';

const isKioskEligiblePath = (pathname: string): boolean => {
  return pathname.startsWith('/shop-floor') || pathname.startsWith('/kiosk') || pathname === '/login';
};

export function syncKioskMode(pathname: string, search: string): boolean {
  const params = new URLSearchParams(search);
  const kioskParam = params.get('kiosk');

  if (kioskParam === '1') {
    localStorage.setItem(KIOSK_STORAGE_KEY, '1');
    return true;
  }

  if (kioskParam === '0') {
    localStorage.removeItem(KIOSK_STORAGE_KEY);
    return false;
  }

  if (!isKioskEligiblePath(pathname)) {
    localStorage.removeItem(KIOSK_STORAGE_KEY);
    return false;
  }

  return localStorage.getItem(KIOSK_STORAGE_KEY) === '1';
}

export function isKioskMode(pathname: string, search: string): boolean {
  const params = new URLSearchParams(search);
  const kioskParam = params.get('kiosk');

  if (kioskParam === '1') return true;
  if (kioskParam === '0') return false;

  if (!isKioskEligiblePath(pathname)) return false;

  return localStorage.getItem(KIOSK_STORAGE_KEY) === '1';
}

export function getKioskDept(search: string): string | null {
  const params = new URLSearchParams(search);
  return params.get('dept');
}

export function getKioskWorkCenterId(search: string): number | null {
  const params = new URLSearchParams(search);
  const raw = params.get('work_center_id');
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function getKioskWorkCenterCode(search: string): string | null {
  const params = new URLSearchParams(search);
  return params.get('work_center_code');
}

/**
 * Crew-station id from ?station=N. Presence of this param is what routes
 * /kiosk into the crew-station mode (station PIN auth) instead of the
 * single-operator badge-login kiosk.
 */
export function getKioskStationId(search: string): number | null {
  const raw = new URLSearchParams(search).get('station');
  if (!raw || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/** Default idle auto-logout for the operator kiosk (seconds). */
export const KIOSK_IDLE_LOGOUT_DEFAULT_S = 240;
/** Floor so a typo can't make the kiosk log out mid-scan. */
export const KIOSK_IDLE_LOGOUT_MIN_S = 30;
/**
 * Ceiling kept safely below the global AuthContext idle redirect (15 min),
 * so the kiosk's badge screen — not a hard /login redirect — always wins.
 */
export const KIOSK_IDLE_LOGOUT_MAX_S = 600;

/** Read the ?idle_logout_s=N override; clamped to [30, 600], default 240. */
export function getKioskIdleLogoutSeconds(search: string): number {
  const raw = new URLSearchParams(search).get('idle_logout_s');
  if (!raw) return KIOSK_IDLE_LOGOUT_DEFAULT_S;
  const value = Number(raw);
  if (!Number.isFinite(value)) return KIOSK_IDLE_LOGOUT_DEFAULT_S;
  return Math.min(KIOSK_IDLE_LOGOUT_MAX_S, Math.max(KIOSK_IDLE_LOGOUT_MIN_S, Math.round(value)));
}
