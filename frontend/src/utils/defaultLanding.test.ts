import { DEFAULT_LANDING_BY_ROLE, getDefaultLandingPath } from './defaultLanding';

describe('getDefaultLandingPath', () => {
  it('routes managerial roles to the Action Inbox', () => {
    expect(getDefaultLandingPath('admin')).toBe('/action-inbox');
    expect(getDefaultLandingPath('manager')).toBe('/action-inbox');
    expect(getDefaultLandingPath('supervisor')).toBe('/action-inbox');
  });

  it('keeps operators on the kiosk shop-floor screen', () => {
    expect(getDefaultLandingPath('operator')).toBe('/shop-floor/operations?kiosk=1');
  });

  it('falls back to the classic dashboard for other and unknown roles', () => {
    expect(getDefaultLandingPath('quality')).toBe('/');
    expect(getDefaultLandingPath('shipping')).toBe('/');
    expect(getDefaultLandingPath('viewer')).toBe('/');
    expect(getDefaultLandingPath('platform_admin')).toBe('/');
    expect(getDefaultLandingPath('something_new')).toBe('/');
  });

  it('falls back to the classic dashboard when the role is missing', () => {
    expect(getDefaultLandingPath(undefined)).toBe('/');
    expect(getDefaultLandingPath(null)).toBe('/');
    expect(getDefaultLandingPath('')).toBe('/');
  });

  it('only maps known roles (guards against accidental additions)', () => {
    expect(Object.keys(DEFAULT_LANDING_BY_ROLE).sort()).toEqual(['admin', 'manager', 'operator', 'supervisor']);
  });
});
