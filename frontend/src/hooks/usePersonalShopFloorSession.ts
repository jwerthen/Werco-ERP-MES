import { useCallback, useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';

type SessionUser = { id: number; company_id?: number } | null | undefined;
export const SHOP_FLOOR_SESSION_CHANGED = 'werco:shop-floor-session-changed';
const DEFAULT_IDLE_MS = 15 * 60 * 1000;
const PERSONAL_IDLE_MS = 30 * 60 * 1000;
let activePhoneScope: string | null = null;

function scopeKey(user: SessionUser): string | null {
  return user &&
    Number.isSafeInteger(user.id) &&
    user.id > 0 &&
    Number.isSafeInteger(user.company_id) &&
    Number(user.company_id) > 0
    ? `shop_floor_personal_phone:v1:company:${user.company_id}:user:${user.id}`
    : null;
}

function readPreference(key: string | null): boolean {
  try {
    return key !== null && localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function isPersonalPhoneSurface(pathname: string, search: string): boolean {
  try {
    // ?kiosk=1 on /shop-floor is the simplified phone navigation, not the
    // shared /kiosk station. Only an explicit personal-phone choice opts in.
    return (
      (pathname === '/shop-floor' || pathname.startsWith('/shop-floor/')) &&
      !new URLSearchParams(search).has('station') &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(max-width: 767px)').matches
    );
  } catch {
    return false;
  }
}

/** AuthContext also checks the current URL; a saved preference never extends a kiosk. */
export function getShopFloorIdleTimeoutMs(user: SessionUser): number {
  const key = scopeKey(user);
  return key &&
    activePhoneScope === key &&
    readPreference(key) &&
    isPersonalPhoneSurface(window.location.pathname, window.location.search)
    ? PERSONAL_IDLE_MS
    : DEFAULT_IDLE_MS;
}

/** Explicit, per-operator opt-in. Mount only on the Shop Floor screen. */
export function usePersonalShopFloorSession(user: SessionUser) {
  const location = useLocation();
  const key = scopeKey(user);
  const [revision, setRevision] = useState(0);
  const [phoneSize, setPhoneSize] = useState(
    () => typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches
  );
  const available = Boolean(key && phoneSize && isPersonalPhoneSurface(location.pathname, location.search));
  const personalPhone = readPreference(key);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(max-width: 767px)');
    const update = () => setPhoneSize(query.matches);
    query.addEventListener?.('change', update);
    update();
    return () => query.removeEventListener?.('change', update);
  }, []);

  useEffect(() => {
    activePhoneScope = available ? key : null;
    window.dispatchEvent(new Event(SHOP_FLOOR_SESSION_CHANGED));
    return () => {
      if (activePhoneScope === key) activePhoneScope = null;
      window.dispatchEvent(new Event(SHOP_FLOOR_SESSION_CHANGED));
    };
  }, [available, key, revision]);

  const setPersonalPhone = useCallback(
    (enabled: boolean) => {
      if (!key || !available) return;
      try {
        if (enabled) localStorage.setItem(key, '1');
        else localStorage.removeItem(key);
      } catch {
        // Keep the default timeout when the browser cannot persist explicit consent.
      }
      setRevision(value => value + 1);
    },
    [available, key]
  );

  return { available, personalPhone, setPersonalPhone, timeoutMinutes: available && personalPhone ? 30 : 15 };
}
