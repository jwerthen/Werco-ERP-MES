import {
  getKioskIdleLogoutSeconds,
  isKioskMode,
  KIOSK_IDLE_LOGOUT_DEFAULT_S,
  KIOSK_IDLE_LOGOUT_MAX_S,
  KIOSK_IDLE_LOGOUT_MIN_S,
} from './kiosk';

describe('kiosk utils', () => {
  afterEach(() => {
    localStorage.clear();
  });

  describe('isKioskMode eligibility', () => {
    it('treats /kiosk as kiosk-eligible (sticky mode survives without the param)', () => {
      localStorage.setItem('kiosk_mode', '1');
      expect(isKioskMode('/kiosk', '')).toBe(true);
      expect(isKioskMode('/kiosk', '?work_center_id=7')).toBe(true);
    });

    it('still drops kiosk mode on non-eligible paths', () => {
      localStorage.setItem('kiosk_mode', '1');
      expect(isKioskMode('/work-orders', '')).toBe(false);
    });
  });

  describe('getKioskIdleLogoutSeconds', () => {
    it('defaults to 4 minutes', () => {
      expect(getKioskIdleLogoutSeconds('')).toBe(KIOSK_IDLE_LOGOUT_DEFAULT_S);
      expect(KIOSK_IDLE_LOGOUT_DEFAULT_S).toBe(240);
    });

    it('honors the ?idle_logout_s override', () => {
      expect(getKioskIdleLogoutSeconds('?idle_logout_s=120')).toBe(120);
    });

    it('clamps to a sane range and ignores junk', () => {
      expect(getKioskIdleLogoutSeconds('?idle_logout_s=5')).toBe(KIOSK_IDLE_LOGOUT_MIN_S);
      expect(getKioskIdleLogoutSeconds('?idle_logout_s=99999')).toBe(KIOSK_IDLE_LOGOUT_MAX_S);
      expect(getKioskIdleLogoutSeconds('?idle_logout_s=banana')).toBe(KIOSK_IDLE_LOGOUT_DEFAULT_S);
    });
  });
});
