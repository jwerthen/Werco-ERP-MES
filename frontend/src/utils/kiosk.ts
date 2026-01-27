export const KIOSK_STORAGE_KEY = 'kiosk_mode';

export function syncKioskMode(search: string): boolean {
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

  return localStorage.getItem(KIOSK_STORAGE_KEY) === '1';
}

export function isKioskMode(search: string): boolean {
  const params = new URLSearchParams(search);
  const kioskParam = params.get('kiosk');

  if (kioskParam === '1') return true;
  if (kioskParam === '0') return false;

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
