import React, { useEffect, useState } from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal } from './ui/Modal';
import { useAuth } from '../context/AuthContext';

export default function SessionWarningModal() {
  const { sessionWarning, sessionWarningExpiresAt, extendSession, logout } = useAuth();
  const [countdown, setCountdown] = useState(60);

  useEffect(() => {
    if (!sessionWarning) {
      setCountdown(60);
      return;
    }

    const deadline = sessionWarningExpiresAt ?? Date.now() + 60_000;
    const updateCountdown = () => setCountdown(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    updateCountdown();
    const timer = setInterval(updateCountdown, 1000);
    document.addEventListener('visibilitychange', updateCountdown);

    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', updateCountdown);
    };
  }, [sessionWarning, sessionWarningExpiresAt]);

  if (!sessionWarning) return null;

  return (
    <Modal
      open={sessionWarning}
      onClose={extendSession}
      size="md"
      ariaLabelledBy="session-warning-title"
      closeOnBackdrop={false}
      closeOnEscape={false}
      padded={false}
    >
      <div className="du-modal-box max-w-md p-0 overflow-hidden">
        <div className="du-alert du-alert-warning rounded-none border-0 border-b border-amber-200/60">
          <ExclamationTriangleIcon className="h-6 w-6" />
          <h3 id="session-warning-title" className="text-lg font-semibold">
            Still working?
          </h3>
        </div>

        <div className="px-6 py-5 space-y-4">
          <p className="text-base-content/80">Tap to stay signed in and continue your work.</p>
          <p className="text-base-content/70">
            You will be logged out in <span className="du-badge du-badge-warning font-bold">{countdown}s</span>.
          </p>
          <p className="text-sm text-base-content/60">
            If you are signed out, sign in with your own badge to return to your saved shop-floor job.
          </p>
        </div>

        <div className="du-modal-action mt-0 px-6 py-4 bg-base-200/60 border-t border-base-300 flex-col-reverse sm:flex-row justify-end gap-3">
          <button onClick={logout} data-session-logout className="du-btn du-btn-ghost min-h-12">
            Log Out Now
          </button>
          <button onClick={extendSession} className="du-btn du-btn-primary min-h-12">
            Stay Logged In
          </button>
        </div>
      </div>
    </Modal>
  );
}
