import React, { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { User } from '../types';
import api from '../services/api';
import { setCustomPermissions } from '../utils/permissions';
import { getShopFloorIdleTimeoutMs, SHOP_FLOOR_SESSION_CHANGED } from '../hooks/usePersonalShopFloorSession';

// Warning before timeout (1 minute before)
const IDLE_WARNING = 60 * 1000;

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  sessionWarning: boolean;
  sessionWarningExpiresAt: number | null;
  // `identifier` is an email OR an employee ID -- POST /auth/login resolves either,
  // so this is deliberately not called `email`.
  login: (identifier: string, password: string) => Promise<void>;
  loginWithEmployeeId: (employeeId: string) => Promise<void>;
  logout: () => void;
  logoutWithEmployeeId: (employeeId: string) => Promise<void>;
  extendSession: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [sessionWarning, setSessionWarning] = useState(false);
  const [sessionWarningExpiresAt, setSessionWarningExpiresAt] = useState<number | null>(null);

  const idleDeadlineRef = useRef<number | null>(null);
  const idleTimerRef = useRef<NodeJS.Timeout | null>(null);
  const warningTimerRef = useRef<NodeJS.Timeout | null>(null);

  const clearTimers = useCallback(() => {
    if (idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
    if (warningTimerRef.current) {
      clearTimeout(warningTimerRef.current);
      warningTimerRef.current = null;
    }
  }, []);

  const persistUser = useCallback((nextUser: User) => {
    setUser(nextUser);
    sessionStorage.setItem('user', JSON.stringify(nextUser));
  }, []);

  const handleLogoutDueToIdle = useCallback(() => {
    clearTimers();
    idleDeadlineRef.current = null;
    const badgeMode = sessionStorage.getItem('auth_sign_in_method') === 'employee';
    api.logout();
    setUser(null);
    setSessionWarning(false);
    setSessionWarningExpiresAt(null);
    sessionStorage.removeItem('user');
    if (window.location.pathname.startsWith('/kiosk')) return;
    const returnTo = window.location.pathname + window.location.search + window.location.hash;
    window.location.href = `/login?reason=idle${badgeMode ? '&mode=employee' : ''}&returnTo=${encodeURIComponent(returnTo)}`;
  }, [clearTimers]);

  const resetIdleTimer = useCallback(() => {
    if (!user) return;

    clearTimers();
    setSessionWarning(false);
    setSessionWarningExpiresAt(null);
    const idleTimeout = getShopFloorIdleTimeoutMs(user);
    const deadline = Date.now() + idleTimeout;
    idleDeadlineRef.current = deadline;

    // Set warning timer (fires 1 minute before logout)
    warningTimerRef.current = setTimeout(() => {
      setSessionWarning(true);
      setSessionWarningExpiresAt(deadline);
    }, idleTimeout - IDLE_WARNING);

    // Set logout timer
    idleTimerRef.current = setTimeout(() => {
      handleLogoutDueToIdle();
    }, idleTimeout);
  }, [user, clearTimers, handleLogoutDueToIdle]);

  const extendSession = useCallback(() => {
    if (idleDeadlineRef.current !== null && Date.now() >= idleDeadlineRef.current) handleLogoutDueToIdle();
    else resetIdleTimer();
  }, [resetIdleTimer, handleLogoutDueToIdle]);

  // Track user activity
  useEffect(() => {
    if (!user) return;

    const activityEvents = ['pointerdown', 'mousedown', 'keydown', 'touchstart', 'wheel'];

    const handleActivity = (event: Event) => {
      // Let the warning's explicit sign-out button receive its click before
      // dismissing the modal on pointerdown/keydown.
      if (event.target instanceof Element && event.target.closest('[data-session-logout]')) return;
      extendSession();
    };
    // Mobile browsers suspend timers while the phone sleeps. Re-check elapsed
    // wall time before accepting an activity event or revealing an old session.
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible' || idleDeadlineRef.current === null) return;
      if (Date.now() >= idleDeadlineRef.current) handleLogoutDueToIdle();
      else if (idleDeadlineRef.current - Date.now() <= IDLE_WARNING) {
        setSessionWarning(true);
        setSessionWarningExpiresAt(idleDeadlineRef.current);
      }
    };

    activityEvents.forEach(event => {
      window.addEventListener(event, handleActivity);
    });
    document.addEventListener('visibilitychange', handleVisibility);
    window.addEventListener(SHOP_FLOOR_SESSION_CHANGED, handleActivity);

    // Start the idle timer
    resetIdleTimer();

    return () => {
      activityEvents.forEach(event => {
        window.removeEventListener(event, handleActivity);
      });
      document.removeEventListener('visibilitychange', handleVisibility);
      window.removeEventListener(SHOP_FLOOR_SESSION_CHANGED, handleActivity);
      clearTimers();
    };
  }, [user, resetIdleTimer, extendSession, handleLogoutDueToIdle, clearTimers]);

  // Kiosk badge-screen fallback wiring: on /kiosk paths the axios 401
  // interceptor clears the session WITHOUT navigating to /login
  // (services/api.ts → redirectToLoginUnlessKiosk). React to the token
  // clearing here so `isAuthenticated` actually flips and OperatorKiosk
  // re-renders to its badge login screen without a reload. api.setToken /
  // setTokens dispatch the same event with a token present — no-ops here.
  useEffect(() => {
    const handleTokenChanged = () => {
      let token: string | null = null;
      try {
        token = sessionStorage.getItem('token');
      } catch {
        // sessionStorage unavailable — treat as signed out
      }
      if (!token) {
        clearTimers();
        idleDeadlineRef.current = null;
        setUser(null);
        setSessionWarning(false);
        setSessionWarningExpiresAt(null);
        try {
          sessionStorage.removeItem('user');
        } catch {
          // nothing to clear
        }
      }
    };
    window.addEventListener('werco:auth-token-changed', handleTokenChanged);
    return () => window.removeEventListener('werco:auth-token-changed', handleTokenChanged);
  }, [clearTimers]);

  useEffect(() => {
    // Check for existing token on mount
    const token = sessionStorage.getItem('token');
    const savedUser = sessionStorage.getItem('user');

    const restoreSession = async () => {
      if (!token || !savedUser) {
        setIsLoading(false);
        return;
      }

      try {
        const parsed = JSON.parse(savedUser);
        // Validate minimum shape. Older sessions stored only a partial user,
        // so refresh the full profile before trusting it for kiosk/logout UI.
        if (
          parsed &&
          typeof parsed === 'object' &&
          typeof parsed.id !== 'undefined' &&
          typeof parsed.email === 'string' &&
          typeof parsed.role === 'string'
        ) {
          const hasFullUserShape =
            typeof parsed.employee_id === 'string' &&
            typeof parsed.first_name === 'string' &&
            typeof parsed.last_name === 'string' &&
            typeof parsed.is_active === 'boolean';

          if (hasFullUserShape) {
            setUser(parsed);
          }

          try {
            const currentUser = await api.getCurrentUser();
            persistUser({ ...parsed, ...currentUser });
          } catch {
            if (hasFullUserShape) {
              setUser(parsed);
            } else {
              throw new Error('Stored user is incomplete and could not be refreshed');
            }
          }
        } else {
          throw new Error('Stored user is missing required fields');
        }
      } catch {
        sessionStorage.removeItem('user');
        sessionStorage.removeItem('token');
        sessionStorage.removeItem('refreshToken');
        sessionStorage.removeItem('tokenExpiresAt');
      }
      setIsLoading(false);
    };

    restoreSession();
  }, [persistUser]);

  const login = async (identifier: string, password: string) => {
    const response = await api.login(identifier, password);
    // Use setTokens for new refresh token flow, fallback to setToken for backwards compatibility
    if (response.refresh_token && response.expires_in) {
      api.setTokens(response.access_token, response.refresh_token, response.expires_in);
    } else {
      api.setToken(response.access_token);
    }
    persistUser(response.user);
    sessionStorage.setItem('auth_sign_in_method', 'password');

    // Load custom role permissions from backend (non-blocking)
    try {
      const permData = await api.getRolePermissions();
      if (permData?.role_permissions) {
        setCustomPermissions(permData.role_permissions);
      }
    } catch {
      // Permissions loading failed - use defaults
      console.warn('Failed to load custom permissions, using defaults');
    }
  };

  const loginWithEmployeeId = async (employeeId: string) => {
    const response = await api.loginWithEmployeeId(employeeId);
    if (response.refresh_token && response.expires_in) {
      api.setTokens(response.access_token, response.refresh_token, response.expires_in);
    } else {
      api.setToken(response.access_token);
    }
    persistUser(response.user);
    sessionStorage.setItem('auth_sign_in_method', 'employee');

    try {
      const permData = await api.getRolePermissions();
      if (permData?.role_permissions) {
        setCustomPermissions(permData.role_permissions);
      }
    } catch {
      console.warn('Failed to load custom permissions, using defaults');
    }
  };

  const logout = () => {
    clearTimers();
    idleDeadlineRef.current = null;
    api.logout();
    setUser(null);
    setSessionWarning(false);
    setSessionWarningExpiresAt(null);
    sessionStorage.removeItem('user');
  };

  const logoutWithEmployeeId = async (employeeId: string) => {
    if (!user) {
      logout();
      return;
    }

    const activeEmployeeId = user.employee_id || '';
    if (!activeEmployeeId) {
      throw new Error('Active user is missing an employee ID. Refresh and try again.');
    }

    const enteredId = employeeId.trim();
    const exactMatch = activeEmployeeId.toLowerCase() === enteredId.toLowerCase();
    const userDigits = activeEmployeeId.replace(/\D/g, '');
    const userBadgeId = userDigits ? userDigits.slice(-4).padStart(4, '0') : null;
    const enteredDigits = enteredId.replace(/\D/g, '');
    const enteredBadgeId = enteredDigits ? enteredDigits.slice(-4).padStart(4, '0') : null;
    const badgeMatch = userBadgeId !== null && enteredBadgeId !== null && userBadgeId === enteredBadgeId;

    if (!exactMatch && !badgeMatch) {
      throw new Error('Employee ID does not match the active user');
    }
    try {
      await api.logoutWithEmployeeId(enteredId);
    } finally {
      logout();
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isLoading,
        sessionWarning,
        sessionWarningExpiresAt,
        login,
        loginWithEmployeeId,
        logout,
        logoutWithEmployeeId,
        extendSession,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
