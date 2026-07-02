/**
 * /visitor-signin — standalone full-screen visitor sign-in tablet.
 *
 * Deliberately outside the app shell (NO Layout, NO PrivateRoute), like /kiosk
 * and /wallboard. Auth is a shared-PIN STATION token: staff enter the station
 * PIN once (?station=<id>), the tablet mints a scoped `type="signin"` JWT (24h)
 * via POST /visitor-logs/station-login and holds it in sessionStorage through
 * the isolated `signinClient` — the token NEVER enters the global axios client
 * (whose 401 interceptor would force-redirect to /login, fatal on a tablet).
 *
 * Flow: PIN keypad → welcome (Sign In / Sign Out) → sign-in form / sign-out
 * lookup → done. Idle resets the form to welcome and DISCARDS half-entered data
 * (privacy) while KEEPING the token. "Lock station" returns to the PIN screen.
 *
 * Writes are NON-OPTIMISTIC: loading state, reflect only the server response,
 * surface the server's verbatim error `detail` via toast. Offline hard-disables
 * submit (never queue) with a role=alert banner referenced via aria-describedby.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  ArrowLeftCircleIcon,
  ArrowRightOnRectangleIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
  SignalSlashIcon,
} from '@heroicons/react/24/solid';
import KioskKeypad from '../components/kiosk/KioskKeypad';
import { useKioskIdleLogout } from '../hooks/useKioskIdleLogout';
import { useToast } from '../components/ui/Toast';
import {
  SigninApiError,
  clearStationToken,
  getStationToken,
  postSignIn,
  postSignOut,
  stationLogin,
} from '../services/signinClient';
import { PURPOSE_TILES } from '../components/visitor/visitorConstants';
import { formatCentralDateTime } from '../utils/centralTime';
import type { VisitorLogResponse, VisitorPurpose, VisitorSignOut409, VisitorSignOutMatch } from '../types/visitor';

const PIN_MIN = 4;
const PIN_MAX = 8;
// Visitors browse, fill, hesitate — give them longer than the shop kiosk before
// privacy-wiping the form. Token TTL (24h) is the real session backstop.
const IDLE_RESET_SECONDS = 120;

type TabletView =
  | { name: 'signIn' }
  | { name: 'signOut' }
  | { name: 'signOutPicker'; name_query: string; matches: VisitorSignOutMatch[] }
  | { name: 'done'; kind: 'in' | 'out'; record: VisitorLogResponse };

interface SignInForm {
  visitor_name: string;
  visitor_company: string;
  visitor_phone: string;
  host_name: string;
  purpose: VisitorPurpose | null;
  purpose_note: string;
  safety_acknowledged: boolean;
}

const EMPTY_FORM: SignInForm = {
  visitor_name: '',
  visitor_company: '',
  visitor_phone: '',
  host_name: '',
  purpose: null,
  purpose_note: '',
  safety_acknowledged: false,
};

const OFFLINE_HINT_ID = 'visitor-offline-hint';

// Signed-in timestamp on the sign-out picker: shop-local Central, no year
// (matches the original month/day/time layout).
function formatSignedInAt(iso: string): string {
  return formatCentralDateTime(iso, { year: undefined });
}

/** Pull a 409 disambiguation body out of a SigninApiError, if shaped like one. */
function as409(err: unknown): VisitorSignOut409 | null {
  if (!(err instanceof SigninApiError) || err.status !== 409) return null;
  const detail = err.detail as Partial<VisitorSignOut409> | undefined;
  if (detail && Array.isArray(detail.matches)) {
    return { message: detail.message ?? 'Multiple matches found.', matches: detail.matches };
  }
  return null;
}

export default function VisitorSignIn() {
  const [searchParams] = useSearchParams();
  const stationParam = searchParams.get('station');
  const stationId = stationParam != null && /^\d+$/.test(stationParam) ? Number(stationParam) : null;

  const { showToast } = useToast();

  // Session: do we hold a station token? (PIN screen shows until we do.)
  const [hasToken, setHasToken] = useState<boolean>(() => getStationToken() != null);
  const [stationLabel, setStationLabel] = useState<string | null>(null);

  // PIN keypad state.
  const [pin, setPin] = useState('');
  const [pinSubmitting, setPinSubmitting] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);

  // Welcome / form navigation.
  const [view, setView] = useState<TabletView>({ name: 'signIn' });
  const [stage, setStage] = useState<'welcome' | 'form'>('welcome');
  const [form, setForm] = useState<SignInForm>(EMPTY_FORM);
  const [signOutName, setSignOutName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Connectivity — disable writes while offline (never queue).
  const [online, setOnline] = useState<boolean>(() => (typeof navigator === 'undefined' ? true : navigator.onLine));
  useEffect(() => {
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
    };
  }, []);

  // --- Reset back to the welcome screen, discarding any half-entered data. ----
  const resetToWelcome = useCallback(() => {
    setForm(EMPTY_FORM);
    setSignOutName('');
    setView({ name: 'signIn' });
    setStage('welcome');
  }, []);

  // Idle: privacy-wipe the form + return to welcome, but KEEP the token so the
  // next visitor can keep self-serving. Only armed once a PIN session exists.
  const { countdownSeconds } = useKioskIdleLogout({
    enabled: hasToken,
    timeoutSeconds: IDLE_RESET_SECONDS,
    onTimeout: resetToWelcome,
  });

  // "Lock station" — drop the token and return to the PIN screen.
  const lockStation = useCallback(() => {
    clearStationToken();
    setHasToken(false);
    setPin('');
    setPinError(null);
    setStationLabel(null);
    resetToWelcome();
  }, [resetToWelcome]);

  // --- PIN login --------------------------------------------------------------
  const submitPin = useCallback(async () => {
    if (stationId == null || pinSubmitting) return;
    if (pin.length < PIN_MIN || pin.length > PIN_MAX) {
      setPinError(`Enter the ${PIN_MIN}–${PIN_MAX} digit station PIN.`);
      return;
    }
    setPinSubmitting(true);
    setPinError(null);
    try {
      const res = await stationLogin(stationId, pin);
      setStationLabel(res.station_label);
      setHasToken(true);
      setPin('');
      resetToWelcome();
    } catch (err) {
      const message =
        err instanceof SigninApiError ? err.message : 'Could not reach the server. Check the connection and try again.';
      setPinError(message);
      setPin('');
    } finally {
      setPinSubmitting(false);
    }
  }, [stationId, pin, pinSubmitting, resetToWelcome]);

  // Writes are blocked while a request is in flight OR while offline. Firing a
  // sign-in/out against a dead connection silently drops the record, so we
  // hard-disable submit; the offline banner (OFFLINE_HINT_ID) is the accessible
  // explanation, referenced via aria-describedby.
  const writesBlocked = submitting || !online;

  // --- Sign-in submit (non-optimistic) ---------------------------------------
  const canSubmitSignIn =
    form.visitor_name.trim().length > 0 &&
    form.purpose != null &&
    (form.purpose !== 'other' || form.purpose_note.trim().length > 0) &&
    form.safety_acknowledged;

  const submitSignIn = useCallback(async () => {
    if (!canSubmitSignIn || form.purpose == null || writesBlocked) return;
    setSubmitting(true);
    try {
      const record = await postSignIn({
        visitor_name: form.visitor_name.trim(),
        visitor_company: form.visitor_company.trim() || undefined,
        visitor_phone: form.visitor_phone.trim() || undefined,
        host_name: form.host_name.trim() || undefined,
        purpose: form.purpose,
        purpose_note: form.purpose === 'other' ? form.purpose_note.trim() : undefined,
        safety_acknowledged: form.safety_acknowledged,
      });
      setForm(EMPTY_FORM);
      setView({ name: 'done', kind: 'in', record });
      setStage('form');
    } catch (err) {
      // Surface the server's verbatim detail; keep the form (and its data) intact.
      const message = err instanceof SigninApiError ? err.message : 'Could not sign in. Please try again.';
      showToast('error', message);
    } finally {
      setSubmitting(false);
    }
  }, [canSubmitSignIn, form, writesBlocked, showToast]);

  // --- Sign-out submit (name lookup, with 409 picker) ------------------------
  const submitSignOut = useCallback(
    async (payload: { name?: string; visitor_log_id?: number }, nameForPicker: string) => {
      if (writesBlocked) return;
      setSubmitting(true);
      try {
        const record = await postSignOut(payload);
        setSignOutName('');
        setView({ name: 'done', kind: 'out', record });
        setStage('form');
      } catch (err) {
        const ambiguity = as409(err);
        if (ambiguity) {
          // Name matched >1 open visit — show the picker, then re-POST by id.
          setView({ name: 'signOutPicker', name_query: nameForPicker, matches: ambiguity.matches });
          setStage('form');
          return;
        }
        const message = err instanceof SigninApiError ? err.message : 'Could not sign out. Please try again.';
        showToast('error', message);
      } finally {
        setSubmitting(false);
      }
    },
    [writesBlocked, showToast]
  );

  const stationName = useMemo(
    () => stationLabel || (stationId != null ? `Station #${stationId}` : 'Visitor Station'),
    [stationLabel, stationId]
  );

  // --- Guard: no station id in the URL ----------------------------------------
  if (stationId == null) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 text-center">
        <ExclamationTriangleIcon className="h-16 w-16 text-fd-amber" />
        <h1 className="mt-4 text-3xl font-bold text-fd-ink">Station not configured</h1>
        <p className="mt-3 max-w-xl text-lg text-fd-body">
          Open this tablet with a station URL, e.g.{' '}
          <code className="rounded bg-fd-sunken px-2 py-1 font-mono text-fd-cyan">/visitor-signin?station=1</code>
        </p>
        <p className="mt-3 max-w-xl text-sm text-fd-mute">
          Create a station and copy its link from the Visitor Log admin page → Stations.
        </p>
      </div>
    );
  }

  // --- PIN unlock screen ------------------------------------------------------
  if (!hasToken) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 py-10">
        <div className="w-full max-w-md">
          <div className="mb-8 text-center">
            <p className="font-mono text-sm uppercase tracking-[0.3em] text-fd-mute">{stationName}</p>
            <h1 className="mt-3 flex items-center justify-center gap-3 text-4xl font-bold text-fd-ink">
              <LockClosedIcon className="h-8 w-8 text-fd-blue" aria-hidden="true" />
              Enter station PIN
            </h1>
            <p className="mt-2 text-lg text-fd-body">
              Ask reception for the {PIN_MIN}–{PIN_MAX} digit PIN
            </p>
          </div>

          <div
            data-testid="visitor-pin-display"
            className="mb-4 flex min-h-20 items-center justify-center rounded border border-fd-line-bright bg-fd-sunken px-4"
          >
            {pin ? (
              <span className="font-mono text-5xl font-semibold tracking-[0.4em] text-fd-ink">
                {'•'.repeat(pin.length)}
              </span>
            ) : (
              <span className="text-xl text-fd-faint">Enter PIN…</span>
            )}
          </div>

          {pinError && (
            <div
              role="alert"
              className="mb-4 w-full rounded border border-fd-red bg-fd-red/10 px-4 py-4 text-center text-xl font-semibold text-fd-red"
            >
              {pinError}
            </div>
          )}

          <KioskKeypad
            value={pin}
            onChange={setPin}
            maxLength={PIN_MAX}
            disabled={pinSubmitting}
            idPrefix="visitor-pin-key"
          />

          <button
            type="button"
            onClick={() => void submitPin()}
            disabled={pinSubmitting || pin.length < PIN_MIN}
            className="mt-4 flex min-h-20 w-full items-center justify-center gap-3 rounded border border-werco-navy-600 bg-werco-navy-600 text-2xl font-bold uppercase tracking-wider text-white transition-colors hover:bg-werco-navy-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {pinSubmitting ? 'Unlocking…' : 'Unlock'}
          </button>
        </div>
      </div>
    );
  }

  // --- Unlocked tablet --------------------------------------------------------
  return (
    <div className="flex min-h-screen flex-col bg-fd-canvas">
      {/* Station header — always visible */}
      <header className="sticky top-0 z-30 border-b border-fd-line bg-fd-panel px-5 py-3">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="truncate font-mono text-xl font-bold tracking-tight text-fd-ink">{stationName}</p>
            <p className="truncate text-sm text-fd-mute">Visitor sign-in</p>
          </div>
          <div className="flex items-center gap-3">
            {!online && (
              <span className="flex items-center gap-2 rounded border border-fd-red bg-fd-red/10 px-3 py-2 font-mono text-sm font-bold uppercase tracking-widest text-fd-red">
                <SignalSlashIcon className="h-5 w-5" aria-hidden="true" />
                Offline
              </span>
            )}
            <button
              type="button"
              onClick={lockStation}
              disabled={submitting}
              className="flex min-h-16 items-center gap-2 rounded border border-fd-line bg-fd-sunken px-4 text-lg font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:cursor-not-allowed disabled:opacity-40"
            >
              <LockClosedIcon className="h-6 w-6" aria-hidden="true" />
              Lock station
            </button>
          </div>
        </div>
      </header>

      {/* Offline banner — also the accessible explanation for disabled submits. */}
      {!online && (
        <div
          role="alert"
          id={OFFLINE_HINT_ID}
          className="border-b border-fd-red bg-fd-red/15 px-5 py-4 text-center text-xl font-bold text-fd-red"
        >
          OFFLINE — sign-in and sign-out are disabled until the connection is restored.
        </div>
      )}

      {/* Idle reset countdown */}
      {countdownSeconds != null && (
        <div
          role="alert"
          data-testid="visitor-idle-countdown"
          className="border-b border-fd-amber bg-fd-amber/15 px-5 py-4 text-center text-xl font-bold text-fd-amber"
        >
          Clearing this form in {countdownSeconds}s — touch the screen to keep going
        </div>
      )}

      <main className="mx-auto w-full max-w-3xl flex-1 space-y-5 px-4 py-6">
        {/* WELCOME */}
        {stage === 'welcome' && (
          <section aria-label="Welcome" className="mx-auto w-full max-w-2xl text-center">
            <h1 className="text-4xl font-bold text-fd-ink">Welcome to Werco</h1>
            <p className="mt-2 text-xl text-fd-body">Are you signing in or signing out?</p>
            <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => {
                  setForm(EMPTY_FORM);
                  setView({ name: 'signIn' });
                  setStage('form');
                }}
                className="flex min-h-40 flex-col items-center justify-center gap-3 rounded border border-fd-green bg-fd-green/10 text-3xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/20"
              >
                <ArrowRightOnRectangleIcon className="h-12 w-12" aria-hidden="true" />
                Sign In
              </button>
              <button
                type="button"
                onClick={() => {
                  setSignOutName('');
                  setView({ name: 'signOut' });
                  setStage('form');
                }}
                className="flex min-h-40 flex-col items-center justify-center gap-3 rounded border border-fd-blue bg-fd-blue/10 text-3xl font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/20"
              >
                <ArrowLeftCircleIcon className="h-12 w-12" aria-hidden="true" />
                Sign Out
              </button>
            </div>
          </section>
        )}

        {/* SIGN-IN FORM */}
        {stage === 'form' && view.name === 'signIn' && (
          <SignInFormView
            form={form}
            setForm={setForm}
            online={online}
            submitting={submitting}
            canSubmit={canSubmitSignIn}
            onSubmit={() => void submitSignIn()}
            onBack={resetToWelcome}
          />
        )}

        {/* SIGN-OUT — name lookup */}
        {stage === 'form' && view.name === 'signOut' && (
          <section aria-label="Sign out" className="mx-auto w-full max-w-xl">
            <h1 className="text-3xl font-bold text-fd-ink">Sign out</h1>
            <p className="mt-1 text-lg text-fd-body">Enter your name to sign out.</p>
            <form
              className="mt-5 space-y-4"
              onSubmit={e => {
                e.preventDefault();
                const name = signOutName.trim();
                if (!name) return;
                void submitSignOut({ name }, name);
              }}
            >
              <label htmlFor="visitor-signout-name" className="label">
                Your name
              </label>
              <input
                id="visitor-signout-name"
                type="text"
                autoComplete="off"
                value={signOutName}
                onChange={e => setSignOutName(e.target.value)}
                placeholder="Jane Smith"
                className="input w-full text-2xl"
              />
              <div className="grid grid-cols-2 gap-3">
                <button
                  type="button"
                  onClick={resetToWelcome}
                  disabled={submitting}
                  className="min-h-20 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
                >
                  Back
                </button>
                <button
                  type="submit"
                  disabled={writesBlocked || !signOutName.trim()}
                  aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                  className="min-h-20 rounded border border-fd-blue bg-fd-blue/15 text-2xl font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {!online ? 'Offline' : submitting ? 'Signing out…' : 'Sign Out'}
                </button>
              </div>
            </form>
          </section>
        )}

        {/* SIGN-OUT — 409 disambiguation picker */}
        {stage === 'form' && view.name === 'signOutPicker' && (
          <section aria-label="Choose your visit" className="mx-auto w-full max-w-xl">
            <h1 className="text-3xl font-bold text-fd-ink">Which visit is yours?</h1>
            <p className="mt-1 text-lg text-fd-body">
              We found more than one open visit for &ldquo;{view.name_query}&rdquo;. Tap yours.
            </p>
            <div className="mt-5 space-y-3">
              {view.matches.map(m => (
                <button
                  key={m.id}
                  type="button"
                  disabled={writesBlocked}
                  aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                  onClick={() => void submitSignOut({ visitor_log_id: m.id }, view.name_query)}
                  className="flex w-full items-center justify-between gap-4 rounded border border-fd-line-bright bg-fd-raised px-5 py-5 text-left transition-colors hover:bg-fd-panel disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <span className="text-2xl font-semibold text-fd-ink">
                    {m.visitor_company || 'No company on file'}
                  </span>
                  <span className="font-mono text-lg text-fd-mute">{formatSignedInAt(m.signed_in_at)}</span>
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={resetToWelcome}
              disabled={submitting}
              className="mt-5 min-h-16 w-full rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
            >
              Cancel
            </button>
          </section>
        )}

        {/* DONE — confirmation */}
        {stage === 'form' && view.name === 'done' && (
          <section aria-label="Done" className="mx-auto w-full max-w-xl text-center">
            <CheckCircleIcon className="mx-auto h-20 w-20 text-fd-green" aria-hidden="true" />
            <h1 className="mt-4 text-4xl font-bold text-fd-ink">{view.kind === 'in' ? 'Signed in' : 'Signed out'}</h1>
            <p className="mt-2 text-2xl text-fd-body">
              {view.kind === 'in' ? 'Welcome, ' : 'Thanks, '}
              <span className="font-semibold text-fd-ink">{view.record.visitor_name}</span>.
            </p>
            {view.kind === 'in' && view.record.host_name && (
              <p className="mt-1 text-lg text-fd-mute">{view.record.host_name} has been notified.</p>
            )}
            <button
              type="button"
              onClick={resetToWelcome}
              className="mt-8 min-h-20 w-full rounded border border-werco-navy-600 bg-werco-navy-600 text-2xl font-bold uppercase tracking-wide text-white transition-colors hover:bg-werco-navy-700"
            >
              Done
            </button>
          </section>
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sign-in form (own component to keep the page readable).
// ---------------------------------------------------------------------------

interface SignInFormViewProps {
  form: SignInForm;
  setForm: React.Dispatch<React.SetStateAction<SignInForm>>;
  online: boolean;
  submitting: boolean;
  canSubmit: boolean;
  onSubmit: () => void;
  onBack: () => void;
}

function SignInFormView({ form, setForm, online, submitting, canSubmit, onSubmit, onBack }: SignInFormViewProps) {
  const writesBlocked = submitting || !online;
  const set = <K extends keyof SignInForm>(key: K, value: SignInForm[K]) =>
    setForm(prev => ({ ...prev, [key]: value }));

  return (
    <section aria-label="Sign in" className="mx-auto w-full max-w-2xl">
      <h1 className="text-3xl font-bold text-fd-ink">Sign in</h1>
      <form
        className="mt-5 space-y-5"
        onSubmit={e => {
          e.preventDefault();
          onSubmit();
        }}
      >
        <div>
          <label htmlFor="visitor-name" className="label">
            Your name
            <span aria-hidden="true" className="ml-0.5 text-fd-red">
              *
            </span>
            <span className="sr-only"> (required)</span>
          </label>
          <input
            id="visitor-name"
            type="text"
            required
            aria-required="true"
            autoComplete="off"
            value={form.visitor_name}
            onChange={e => set('visitor_name', e.target.value)}
            placeholder="Jane Smith"
            className="input w-full text-2xl"
          />
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="visitor-company" className="label">
              Company
            </label>
            <input
              id="visitor-company"
              type="text"
              autoComplete="off"
              value={form.visitor_company}
              onChange={e => set('visitor_company', e.target.value)}
              placeholder="Acme Corp"
              className="input w-full text-xl"
            />
          </div>
          <div>
            <label htmlFor="visitor-phone" className="label">
              Phone
            </label>
            <input
              id="visitor-phone"
              type="tel"
              inputMode="tel"
              autoComplete="off"
              value={form.visitor_phone}
              onChange={e => set('visitor_phone', e.target.value)}
              placeholder="(555) 123-4567"
              className="input w-full text-xl"
            />
          </div>
        </div>

        <div>
          <label htmlFor="visitor-host" className="label">
            Who are you here to see?
          </label>
          <input
            id="visitor-host"
            type="text"
            autoComplete="off"
            value={form.host_name}
            onChange={e => set('host_name', e.target.value)}
            placeholder="Host name"
            className="input w-full text-xl"
          />
        </div>

        {/* Purpose tiles */}
        <fieldset>
          <legend className="label">
            Purpose of visit
            <span aria-hidden="true" className="ml-0.5 text-fd-red">
              *
            </span>
            <span className="sr-only"> (required)</span>
          </legend>
          <div className="mt-1 grid grid-cols-2 gap-3 sm:grid-cols-3">
            {PURPOSE_TILES.map(tile => {
              const selected = form.purpose === tile.value;
              return (
                <button
                  key={tile.value}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => set('purpose', tile.value)}
                  className={`min-h-16 rounded border text-xl font-bold uppercase tracking-wide transition-colors ${
                    selected
                      ? 'border-fd-blue bg-fd-blue/20 text-fd-blue'
                      : 'border-fd-line-bright bg-fd-raised text-fd-body hover:bg-fd-panel'
                  }`}
                >
                  {tile.label}
                </button>
              );
            })}
          </div>
        </fieldset>

        {/* Purpose note — required only when Other */}
        {form.purpose === 'other' && (
          <div>
            <label htmlFor="visitor-purpose-note" className="label">
              Please describe
              <span aria-hidden="true" className="ml-0.5 text-fd-red">
                *
              </span>
              <span className="sr-only"> (required)</span>
            </label>
            <input
              id="visitor-purpose-note"
              type="text"
              required
              aria-required="true"
              autoComplete="off"
              value={form.purpose_note}
              onChange={e => set('purpose_note', e.target.value)}
              placeholder="Reason for your visit"
              className="input w-full text-xl"
            />
          </div>
        )}

        {/* Safety / NDA acknowledgment — GATES submit */}
        <label
          htmlFor="visitor-safety"
          className="flex cursor-pointer items-start gap-3 rounded border border-fd-line-bright bg-fd-raised p-4"
        >
          <input
            id="visitor-safety"
            type="checkbox"
            checked={form.safety_acknowledged}
            onChange={e => set('safety_acknowledged', e.target.checked)}
            className="checkbox mt-1 h-7 w-7"
          />
          <span className="text-lg text-fd-body">
            I acknowledge the site safety rules and the visitor non-disclosure agreement, and agree to be escorted while
            on-site.
          </span>
        </label>

        <div className="grid grid-cols-2 gap-3">
          <button
            type="button"
            onClick={onBack}
            disabled={submitting}
            className="min-h-20 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
          >
            Back
          </button>
          <button
            type="submit"
            disabled={writesBlocked || !canSubmit}
            aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
            className="min-h-20 rounded border border-fd-green bg-fd-green/15 text-2xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {!online ? 'Offline' : submitting ? 'Signing in…' : 'Sign In'}
          </button>
        </div>
      </form>
    </section>
  );
}
