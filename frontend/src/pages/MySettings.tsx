/**
 * My Settings — the per-user notification settings page (`/settings`).
 *
 * PR 4 ships the SMS slice only: mobile number capture, per-event SMS opt-ins,
 * and a test send, all in the self-contained `<SmsSettingsSection>`. PR 3 of the
 * notifications plan fills in the rest of this page — the full in-app / email /
 * digest preference matrix and digest scheduling — by adding sections alongside
 * it rather than rewriting it.
 *
 * Auth-only: every authenticated role manages their own settings here; nothing on
 * this page reads or writes another user's data.
 */

import React from 'react';
import { Link } from 'react-router-dom';
import { BellIcon, Cog6ToothIcon } from '@heroicons/react/24/outline';
import SmsSettingsSection from '../components/settings/SmsSettingsSection';
import { Button } from '../components/ui';

export default function MySettings() {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center">
          <Cog6ToothIcon className="h-8 w-8 text-werco-primary mr-3" aria-hidden="true" />
          <div>
            <h1 className="text-2xl font-bold text-white">My Settings</h1>
            <p className="text-sm text-slate-400">How and where Werco reaches you</p>
          </div>
        </div>
        <Link to="/notifications">
          <Button variant="secondary" size="sm">
            <BellIcon className="mr-1.5 h-4 w-4" aria-hidden="true" />
            View notifications
          </Button>
        </Link>
      </div>

      <SmsSettingsSection />
    </div>
  );
}
