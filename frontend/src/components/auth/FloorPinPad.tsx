import React, { useEffect, useId, useRef, useState } from 'react';
import KioskKeypad from '../kiosk/KioskKeypad';
import { FLOOR_PIN_MAX_LENGTH, FLOOR_PIN_MIN_LENGTH, isValidFloorPinFormat } from '../../services/badgeSignOn';

export interface FloorPinPadProps {
  /**
   * 'kiosk' = large touch keypad (the on-screen pad is the entry; the field
   * suppresses the soft keyboard but still takes a hardware keyboard).
   * 'desk' = a masked field for keyboard entry on the Login page.
   */
  variant: 'kiosk' | 'desk';
  /** The previous attempt's refusal ("Invalid badge or PIN"), or null. */
  error: string | null;
  /** A PIN is with the server: keys, submit and cancel are held. */
  verifying: boolean;
  /** Changes on every new ask — clears the digits and refocuses the field. */
  attempt: number;
  onSubmit: (pin: string) => void;
  onCancel: () => void;
  /** Whose badge this is for (e.g. "0042"), shown so the person knows which sign-in is waiting. */
  badgeLabel?: string | null;
  /** Id for the heading, so a dialog wrapper can point aria-labelledby at it. */
  titleId?: string;
}

const RANGE_TEXT = `${FLOOR_PIN_MIN_LENGTH}–${FLOOR_PIN_MAX_LENGTH} digits`;

/**
 * The floor-PIN pad shown when a badge sign-on answers `floor_pin_required`
 * (AS9100D 8.5.2 — the PIN is what makes the badge holder the person who
 * recorded the work). Masked, digits only, 4–6 long; the server owns every other
 * rule. The digits never leave this component except through `onSubmit`.
 *
 * One component for both surfaces so the two cannot drift: the kiosk wraps it in
 * `KioskFloorPinOverlay`, the desk Login page renders it in place of its form.
 */
export default function FloorPinPad({
  variant,
  error,
  verifying,
  attempt,
  onSubmit,
  onCancel,
  badgeLabel,
  titleId,
}: FloorPinPadProps) {
  const [pin, setPin] = useState('');
  const [formatError, setFormatError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const generatedTitleId = useId();
  const headingId = titleId ?? generatedTitleId;
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();
  const kiosk = variant === 'kiosk';

  // Every new ask (first prompt, or a re-ask after "Invalid badge or PIN") starts empty.
  useEffect(() => {
    setPin('');
    setFormatError(null);
    inputRef.current?.focus();
  }, [attempt]);

  const shownError = formatError ?? error;

  const changePin = (next: string) => {
    if (verifying) return;
    setPin(next.replace(/\D/g, '').slice(0, FLOOR_PIN_MAX_LENGTH));
    setFormatError(null);
  };

  const handleSubmit = (event?: React.FormEvent) => {
    event?.preventDefault();
    if (verifying) return;
    if (!isValidFloorPinFormat(pin)) {
      setFormatError(`Enter your PIN (${RANGE_TEXT}).`);
      inputRef.current?.focus();
      return;
    }
    setFormatError(null);
    onSubmit(pin);
  };

  const describedBy = shownError ? `${hintId} ${errorId}` : hintId;

  if (kiosk) {
    return (
      // Sized to fit a 1024x768 landscape tablet with the error showing: 58px keys
      // (still well over the 44px touch minimum) rather than the 74px sign-in keys.
      <form onSubmit={handleSubmit} className="flex flex-col gap-3 p-5" aria-busy={verifying}>
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-fd-blue">Badge sign-in</p>
          <h2 id={headingId} className="mt-2 text-2xl font-bold leading-tight text-fd-ink">
            Enter your PIN
          </h2>
          {badgeLabel ? (
            <p className="mt-1 font-mono text-sm text-fd-body">
              Badge <span className="font-semibold text-fd-ink">{badgeLabel}</span>
            </p>
          ) : null}
        </div>

        <label htmlFor={inputId} className="sr-only">
          PIN
        </label>
        <input
          ref={inputRef}
          id={inputId}
          type="password"
          // The on-screen pad is the entry: no soft keyboard over it, but a
          // hardware keyboard still types here.
          inputMode="none"
          autoComplete="off"
          maxLength={FLOOR_PIN_MAX_LENGTH}
          value={pin}
          onChange={e => changePin(e.target.value)}
          aria-invalid={shownError ? true : undefined}
          aria-describedby={describedBy}
          readOnly={verifying}
          data-testid="floor-pin-input"
          className={`h-16 w-full rounded-[4px] border bg-fd-sunken px-4 text-center font-mono text-[30px] font-bold tracking-[0.5em] text-fd-ink outline-none focus:border-fd-blue ${
            shownError ? 'border-fd-red' : 'border-fd-line-bright'
          }`}
        />
        <p id={hintId} className="text-center font-mono text-[11px] uppercase tracking-[0.12em] text-fd-mute">
          {verifying ? 'Checking PIN…' : `${RANGE_TEXT} · then Sign in`}
        </p>

        {shownError && (
          <div
            id={errorId}
            role="alert"
            className="rounded-[4px] border border-fd-red bg-fd-red/10 px-4 py-2.5 text-center text-lg font-semibold text-fd-red"
          >
            {shownError}
          </div>
        )}

        <KioskKeypad
          value={pin}
          onChange={changePin}
          maxLength={FLOOR_PIN_MAX_LENGTH}
          disabled={verifying}
          idPrefix="floor-pin-key"
          size="sm"
          allowLeadingZero
        />

        <div className="grid grid-cols-2 gap-2.5">
          <button
            type="button"
            onClick={onCancel}
            disabled={verifying}
            className="h-14 rounded-[4px] border border-fd-line bg-fd-sunken font-mono text-base font-bold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={verifying || pin.length < FLOOR_PIN_MIN_LENGTH}
            className="h-14 rounded-[4px] bg-fd-blue font-mono text-base font-bold uppercase tracking-[0.1em] text-[#04101f] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {verifying ? 'Checking…' : 'Sign in'}
          </button>
        </div>
      </form>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-[18px]" aria-busy={verifying}>
      <div>
        <h3 id={headingId} className="text-lg font-semibold text-fd-ink">
          Enter your PIN
        </h3>
        <p className="mt-1 text-[13.5px] text-fd-mute">
          {badgeLabel ? (
            <>
              Badge <span className="font-mono text-fd-ink">{badgeLabel}</span> needs its floor PIN to sign in.
            </>
          ) : (
            'This badge needs its floor PIN to sign in.'
          )}
        </p>
      </div>

      {shownError && (
        <div id={errorId} role="alert" className="alert alert-danger animate-fade-in text-sm">
          <span>{shownError}</span>
        </div>
      )}

      <div>
        <label
          htmlFor={inputId}
          className="block font-mono text-[10px] uppercase tracking-[0.14em] text-fd-body mb-1.5"
        >
          PIN
        </label>
        <input
          ref={inputRef}
          id={inputId}
          type="password"
          inputMode="numeric"
          autoComplete="off"
          maxLength={FLOOR_PIN_MAX_LENGTH}
          value={pin}
          onChange={e => changePin(e.target.value)}
          aria-invalid={shownError ? true : undefined}
          aria-describedby={describedBy}
          readOnly={verifying}
          data-testid="floor-pin-input"
          className="w-full font-mono text-xl tracking-[0.4em] text-fd-ink rounded-[3px] px-4 py-2.5 outline-none transition-all min-h-[44px] focus:border-fd-blue"
          style={{ background: 'var(--fd-sunken)', border: '1px solid var(--fd-line)' }}
        />
        <p id={hintId} className="font-mono text-[9.5px] tracking-[0.1em] text-fd-mute mt-1.5">
          {verifying ? 'Checking PIN…' : `${RANGE_TEXT}. Ask a supervisor if you have forgotten it.`}
        </p>
      </div>

      <div className="flex gap-2">
        <button type="button" onClick={onCancel} disabled={verifying} className="btn-secondary flex-1">
          Back
        </button>
        <button type="submit" disabled={verifying || pin.length < FLOOR_PIN_MIN_LENGTH} className="btn-primary flex-1">
          {verifying ? (
            <span className="flex items-center justify-center gap-2">
              <span className="spinner h-4 w-4" aria-hidden="true" />
              Checking…
            </span>
          ) : (
            'Sign In'
          )}
        </button>
      </div>
    </form>
  );
}
