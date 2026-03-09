import React, { useEffect, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { useAuth } from '../context/AuthContext';

export default function SessionWarningModal() {
  const { sessionWarning, extendSession, logout } = useAuth();
  const [countdown, setCountdown] = useState(60);

  useEffect(() => {
    if (!sessionWarning) {
      setCountdown(60);
      return;
    }

    const timer = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) {
          clearInterval(timer);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [sessionWarning]);

  if (!sessionWarning) return null;

  return (
    <div className="du-modal du-modal-open">
      <div className="du-modal-box max-w-md p-0 overflow-hidden">
        <div className="du-alert du-alert-warning rounded-none border-0 border-b border-amber-200/60">
          <ExclamationTriangleIcon className="h-6 w-6" />
          <h3 className="text-lg font-semibold">Session Timeout Warning</h3>
        </div>

        <div className="px-6 py-5 space-y-4">
          <p className="text-base-content/80">Your session is about to expire due to inactivity.</p>
          <p className="text-base-content/70">
            You will be logged out in <span className="du-badge du-badge-warning font-bold">{countdown}s</span>.
          </p>
          <p className="text-sm text-base-content/60">Click "Stay Logged In" to continue your session.</p>
        </div>

        <div className="du-modal-action mt-0 px-6 py-4 bg-base-200/60 border-t border-base-300 justify-end gap-3">
          <button onClick={logout} className="du-btn du-btn-ghost">
            Log Out Now
          </button>
          <button onClick={extendSession} className="du-btn du-btn-primary">
            Stay Logged In
          </button>
        </div>
      </div>
    </div>
  );
}
