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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full mx-4 overflow-hidden">
        <div className="bg-amber-50 px-6 py-4 border-b border-amber-200">
          <div className="flex items-center">
            <ExclamationTriangleIcon className="h-6 w-6 text-amber-600 mr-3" />
            <h3 className="text-lg font-semibold text-amber-800">Session Timeout Warning</h3>
          </div>
        </div>
        
        <div className="px-6 py-4">
          <p className="text-gray-700 mb-4">
            Your session is about to expire due to inactivity.
          </p>
          <p className="text-gray-600 mb-4">
            You will be logged out in <span className="font-bold text-amber-600">{countdown}</span> seconds.
          </p>
          <p className="text-sm text-gray-500">
            Click "Stay Logged In" to continue your session.
          </p>
        </div>
        
        <div className="px-6 py-4 bg-gray-50 flex justify-end space-x-3">
          <button
            onClick={logout}
            className="px-4 py-2 text-gray-700 bg-gray-200 rounded-lg hover:bg-gray-300 transition-colors"
          >
            Log Out Now
          </button>
          <button
            onClick={extendSession}
            className="px-4 py-2 text-white bg-cyan-600 rounded-lg hover:bg-cyan-700 transition-colors"
          >
            Stay Logged In
          </button>
        </div>
      </div>
    </div>
  );
}
