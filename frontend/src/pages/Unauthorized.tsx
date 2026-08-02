/**
 * Unauthorized Access Page
 *
 * Displayed when a user tries to access a resource they don't have permission
 * for. Instrument-panel chrome (fd-* tokens, hairline border, sharp corners);
 * navigation stays button+navigate (a "go back" verb, not a link target).
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { usePermissions } from '../hooks/usePermissions';
import { ROLE_LABELS, ROLE_DESCRIPTIONS } from '../utils/permissions';
import { Button } from '../components/ui';
import { usePageTitle } from '../hooks/usePageTitle';

export default function Unauthorized() {
  // Renders outside Layout, so the tab title is set here — otherwise the page
  // the user was refused from keeps titling the tab.
  usePageTitle('Access Denied · Werco ERP');

  const navigate = useNavigate();
  const { role } = usePermissions();

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-4">
      <div className="w-full max-w-md rounded-sm border border-fd-line bg-fd-panel p-6 text-center">
        {/* Icon */}
        <div className="mx-auto mb-3 flex h-14 w-14 items-center justify-center rounded-full border border-fd-red/40 bg-fd-red/10 text-fd-red">
          <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
            />
          </svg>
        </div>

        {/* Title */}
        <h1 className="text-2xl font-bold text-fd-ink">Access Denied</h1>

        {/* Message */}
        <p className="mt-1 text-sm text-fd-body">You don't have permission to access this page.</p>

        {/* User Role Info */}
        {role && (
          <div className="mt-4 rounded-sm border border-fd-amber/40 bg-fd-amber/10 p-3 text-left">
            <span className="block font-mono text-xs uppercase tracking-[0.08em] text-fd-mute">
              Current role
            </span>
            <span className="block font-semibold text-fd-ink">{ROLE_LABELS[role]}</span>
            <span className="mt-0.5 block text-sm text-fd-body">{ROLE_DESCRIPTIONS[role]}</span>
          </div>
        )}

        {/* Actions */}
        <div className="mt-5 flex flex-col justify-center gap-2 sm:flex-row">
          <Button variant="secondary" onClick={() => navigate(-1)}>
            Go Back
          </Button>
          <Button variant="primary" onClick={() => navigate('/')}>
            Go to Dashboard
          </Button>
        </div>

        {/* Contact Admin */}
        <p className="mt-4 text-sm text-fd-mute">
          If you believe you should have access, please contact your administrator.
        </p>
      </div>
    </div>
  );
}
