import {
  clearKioskWorkstation,
  getKioskIdleLogoutSeconds,
  getKioskWorkCenterId,
  isKioskMode,
  KIOSK_IDLE_LOGOUT_DEFAULT_S,
  KIOSK_IDLE_LOGOUT_MAX_S,
  KIOSK_IDLE_LOGOUT_MIN_S,
  readKioskWorkstation,
  saveKioskWorkstation,
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

  describe('workstation preference', () => {
    it('remembers a browser selection within its company only', () => {
      saveKioskWorkstation(4, 7);
      saveKioskWorkstation(5, 12);
      expect(readKioskWorkstation(4)).toBe(7);
      expect(readKioskWorkstation(5)).toBe(12);
      expect(readKioskWorkstation(6)).toBeNull();
      clearKioskWorkstation(4);
      expect(readKioskWorkstation(4)).toBeNull();
      expect(readKioskWorkstation(5)).toBe(12);
    });

    it('does not share a fallback key when the tenant is unknown', () => {
      saveKioskWorkstation(undefined, 7);
      saveKioskWorkstation(null, 8);
      saveKioskWorkstation(0, 9);
      expect(localStorage.length).toBe(0);
      expect(readKioskWorkstation(undefined)).toBeNull();
    });

    it('ignores corrupt saved ids and invalid deep links', () => {
      for (const value of ['-1', '0', '1.5', 'NaN', 'Infinity', 'nope']) {
        localStorage.setItem('kiosk_workstation:company:4', value);
        expect(readKioskWorkstation(4)).toBeNull();
        expect(getKioskWorkCenterId(`?work_center_id=${value}`)).toBeNull();
      }
      expect(getKioskWorkCenterId('?work_center_id=7')).toBe(7);
    });

    it('keeps kiosk setup usable when browser storage is unavailable', () => {
      const read = jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
      const write = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
      const remove = jest.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
      expect(() => saveKioskWorkstation(4, 7)).not.toThrow();
      expect(readKioskWorkstation(4)).toBeNull();
      expect(() => clearKioskWorkstation(4)).not.toThrow();
      read.mockRestore();
      write.mockRestore();
      remove.mockRestore();
    });
  });
});
