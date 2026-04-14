/**
 * Unauthorized Access Page
 *
 * Displayed when a user tries to access a resource they don't have permission for.
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { usePermissions } from '../hooks/usePermissions';
import { ROLE_LABELS, ROLE_DESCRIPTIONS } from '../utils/permissions';

export default function Unauthorized() {
  const navigate = useNavigate();
  const { role } = usePermissions();

  return (
    <div className="min-h-screen bg-gradient-to-br from-[#0d1117] via-[#151b28] to-[#1a1f2e] flex flex-col items-center justify-center px-4">
      <div className="du-card max-w-md w-full bg-base-100 shadow-xl border border-base-300/50">
        <div className="du-card-body text-center">
          {/* Icon */}
          <div className="mx-auto du-avatar placeholder mb-2">
            <div className="bg-red-500/20 text-red-600 rounded-full w-16">
              <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                />
              </svg>
            </div>
          </div>

          {/* Title */}
          <h1 className="text-2xl font-bold text-base-content">Access Denied</h1>

          {/* Message */}
          <p className="text-base-content/70">You don't have permission to access this page.</p>

          {/* User Role Info */}
          {role && (
            <div className="du-alert du-alert-warning text-left">
              <span>
                <span className="block text-xs uppercase tracking-wide opacity-70">Current role</span>
                <span className="block font-semibold">{ROLE_LABELS[role]}</span>
                <span className="block text-sm opacity-80 mt-0.5">{ROLE_DESCRIPTIONS[role]}</span>
              </span>
            </div>
          )}

          {/* Actions */}
          <div className="du-card-actions justify-center flex-col sm:flex-row mt-1">
            <button onClick={() => navigate(-1)} className="du-btn du-btn-outline">
              Go Back
            </button>
            <button onClick={() => navigate('/')} className="du-btn du-btn-primary">
              Go to Dashboard
            </button>
          </div>

          {/* Contact Admin */}
          <p className="text-sm text-base-content/60 mt-2">
            If you believe you should have access, please contact your administrator.
          </p>
        </div>
      </div>
    </div>
  );
}
