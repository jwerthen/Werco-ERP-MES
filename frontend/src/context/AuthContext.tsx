import React, { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';
import { User } from '../types';
import api from '../services/api';

// Idle timeout in milliseconds (15 minutes)
const IDLE_TIMEOUT = 15 * 60 * 1000;
// Warning before timeout (1 minute before)
const IDLE_WARNING = 60 * 1000;

interface MFARequired {
  mfaToken: string;
  message: string;
}

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  sessionWarning: boolean;
  mfaRequired: MFARequired | null;
  login: (email: string, password: string) => Promise<void>;
  verifyMFA: (code: string) => Promise<void>;
  cancelMFA: () => void;
  logout: () => void;
  extendSession: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [sessionWarning, setSessionWarning] = useState(false);
  const [mfaRequired, setMfaRequired] = useState<MFARequired | null>(null);
  
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

  const handleLogoutDueToIdle = useCallback(() => {
    clearTimers();
    api.logout();
    setUser(null);
    setSessionWarning(false);
    localStorage.removeItem('user');
    window.location.href = '/login?reason=idle';
  }, [clearTimers]);

  const resetIdleTimer = useCallback(() => {
    if (!user) return;
    
    clearTimers();
    setSessionWarning(false);
    
    // Set warning timer (fires 1 minute before logout)
    warningTimerRef.current = setTimeout(() => {
      setSessionWarning(true);
    }, IDLE_TIMEOUT - IDLE_WARNING);
    
    // Set logout timer
    idleTimerRef.current = setTimeout(() => {
      handleLogoutDueToIdle();
    }, IDLE_TIMEOUT);
  }, [user, clearTimers, handleLogoutDueToIdle]);

  const extendSession = useCallback(() => {
    resetIdleTimer();
  }, [resetIdleTimer]);

  // Track user activity
  useEffect(() => {
    if (!user) return;

    const activityEvents = ['mousedown', 'keydown', 'scroll', 'touchstart'];
    
    const handleActivity = () => {
      if (!sessionWarning) {
        resetIdleTimer();
      }
    };

    activityEvents.forEach(event => {
      window.addEventListener(event, handleActivity);
    });

    // Start the idle timer
    resetIdleTimer();

    return () => {
      activityEvents.forEach(event => {
        window.removeEventListener(event, handleActivity);
      });
      clearTimers();
    };
  }, [user, sessionWarning, resetIdleTimer, clearTimers]);

  useEffect(() => {
    // Check for existing token on mount
    const token = localStorage.getItem('token');
    const savedUser = localStorage.getItem('user');
    
    if (token && savedUser) {
      try {
        setUser(JSON.parse(savedUser));
      } catch {
        localStorage.removeItem('user');
        localStorage.removeItem('token');
      }
    }
    setIsLoading(false);
  }, []);

  const login = async (email: string, password: string) => {
    const response = await api.login(email, password);
    
    // Check if MFA is required (CMMC Level 2 AC-3.1.1)
    if (response.mfa_required) {
      setMfaRequired({
        mfaToken: response.mfa_token,
        message: response.message
      });
      return; // Don't complete login yet - need MFA verification
    }
    
    // No MFA required or not enabled - complete login
    if (response.refresh_token && response.expires_in) {
      api.setTokens(response.access_token, response.refresh_token, response.expires_in);
    } else {
      api.setToken(response.access_token);
    }
    setUser(response.user);
    localStorage.setItem('user', JSON.stringify(response.user));
  };
  
  const verifyMFA = async (code: string) => {
    if (!mfaRequired) {
      throw new Error('No MFA session active');
    }
    
    const response = await api.verifyMFALogin(mfaRequired.mfaToken, code);
    
    // MFA verified - complete login
    if (response.refresh_token && response.expires_in) {
      api.setTokens(response.access_token, response.refresh_token, response.expires_in);
    } else {
      api.setToken(response.access_token);
    }
    setUser(response.user);
    localStorage.setItem('user', JSON.stringify(response.user));
    setMfaRequired(null);
  };
  
  const cancelMFA = () => {
    setMfaRequired(null);
  };

  const logout = () => {
    api.logout();
    setUser(null);
    localStorage.removeItem('user');
  };

  return (
    <AuthContext.Provider value={{
      user,
      isAuthenticated: !!user,
      isLoading,
      sessionWarning,
      mfaRequired,
      login,
      verifyMFA,
      cancelMFA,
      logout,
      extendSession
    }}>
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
