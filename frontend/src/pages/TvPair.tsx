/**
 * /tv — TV pairing screen for the shop-floor wallboard (A0.5).
 *
 * Nobody types a 355-character #token= URL on a TV remote. Instead, an
 * admin/manager issues an 8-char setup code (Admin Settings → Wallboard
 * Displays), the TV opens /tv (safe as the TV's browser homepage), and the
 * code is entered here. The claim endpoint is PUBLIC (the TV has no
 * credential yet) and codes are single-use, 15-minute, case-insensitive.
 *
 * Standalone like /wallboard: full-screen, NO Layout chrome, NO PrivateRoute,
 * self-contained instrument-panel styling (no Layout-coupled primitives). All
 * network + storage goes through services/wallboardClient — the claimed
 * display token must never enter the global axios auth state.
 *
 * Behavior:
 *  - Already paired (a display token is stored — the user-session token does
 *    NOT count) → straight to /wallboard (+ ?dept= from the stored dept).
 *  - /tv/:code → normalize (uppercase, strip spaces/dashes) and claim
 *    immediately, showing a "Pairing…" state.
 *  - Otherwise: one huge code input (auto-uppercased, grouped XXXX-XXXX,
 *    dashes/spaces ignored) + Connect. Enter submits — TV remotes act as
 *    keyboards, so the whole flow is keyboard-only friendly.
 *  - Claim success → persist token+dept, navigate to /wallboard.
 *  - Claim rejected → "Code not recognized…" (expired/used/unknown are the
 *    same generic 404 by design), input cleared for retry.
 *  - Network failure → distinct "Can't reach the server" message.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  claimDisplayCode,
  getPersistedWallboardDept,
  getStoredDisplayToken,
  normalizeSetupCode,
  persistWallboardToken,
} from '../services/wallboardClient';

const CODE_LENGTH = 8;
const CODE_INPUT_ID = 'tv-pair-code';

/** Group a partial/full code as XXXX-XXXX for TV-legible display. */
function formatCodeForDisplay(normalized: string): string {
  return normalized.length > 4 ? `${normalized.slice(0, 4)}-${normalized.slice(4)}` : normalized;
}

function wallboardPath(dept: string | null): string {
  return dept ? `/wallboard?dept=${encodeURIComponent(dept)}` : '/wallboard';
}

type PairError = 'rejected' | 'network' | null;

export default function TvPair() {
  const navigate = useNavigate();
  const { code: pathCode } = useParams<{ code: string }>();

  const [value, setValue] = useState('');
  const [pairing, setPairing] = useState(false);
  const [error, setError] = useState<PairError>(null);

  const claim = useCallback(
    async (raw: string) => {
      const code = normalizeSetupCode(raw);
      if (code.length !== CODE_LENGTH) return;
      setPairing(true);
      setError(null);
      try {
        const result = await claimDisplayCode(code);
        persistWallboardToken(result.token, result.dept);
        navigate(wallboardPath(result.dept), { replace: true });
      } catch (err) {
        setPairing(false);
        setValue('');
        setError(err instanceof Error && err.message === 'NETWORK' ? 'network' : 'rejected');
        // A failed /tv/<code> deep link must not keep the dead code in the
        // URL — a TV homepage set to that URL would re-claim the burnt code
        // on every reboot. replaceState (not navigate) keeps this mount and
        // its error message alive. (Path-borne codes do reach server access
        // logs, unlike #token= — acceptable: 15-min TTL, single use.)
        if (window.location.pathname !== '/tv') {
          window.history.replaceState(null, '', '/tv');
        }
      }
    },
    [navigate],
  );

  // One-shot bootstrap: already-paired TVs go straight to the board; a
  // /tv/:code deep link claims immediately.
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    if (getStoredDisplayToken()) {
      navigate(wallboardPath(getPersistedWallboardDept()), { replace: true });
      return;
    }
    if (pathCode) {
      void claim(pathCode);
    }
  }, [claim, navigate, pathCode]);

  const normalized = normalizeSetupCode(value);
  const ready = normalized.length === CODE_LENGTH;

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setValue(formatCodeForDisplay(normalizeSetupCode(e.target.value).slice(0, CODE_LENGTH)));
    if (error) setError(null);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!pairing && ready) void claim(value);
  };

  return (
    <div className="fixed inset-0 flex flex-col items-center justify-center overflow-hidden bg-[#070a0f] p-8 font-sans text-[#f0f4f9]">
      <p className="mb-8 text-[1.125rem] font-semibold uppercase tracking-[0.35em] text-[#8b98a9]">
        Werco<span className="text-[#C8352B]">.</span> Shop Wallboard
      </p>

      {pairing ? (
        <div className="flex flex-col items-center gap-4 text-center" data-testid="tv-pair-pairing">
          <p className="text-[3rem] font-bold">Pairing…</p>
          <p className="text-[1.25rem] text-[#8b98a9]">Connecting this display to the wallboard.</p>
        </div>
      ) : (
        <form
          onSubmit={handleSubmit}
          className="flex w-full max-w-[44rem] flex-col items-stretch gap-6 border border-[#243042] bg-[#141b26] px-10 py-12"
          data-testid="tv-pair-form"
        >
          <div className="text-center">
            <h1 className="text-[2.5rem] font-bold leading-tight">Connect this TV</h1>
            <p className="mt-2 text-[1.25rem] text-[#8b98a9]">
              Get a code from Admin Settings → Wallboard Displays.
            </p>
          </div>

          {error && (
            <p
              role="alert"
              className="border border-[#f04438]/60 bg-[#f04438]/10 px-4 py-3 text-center text-[1.5rem] font-semibold text-[#f04438]"
              data-testid="tv-pair-error"
            >
              {error === 'network'
                ? "Can't reach the server — check the network connection and try again."
                : 'Code not recognized — codes expire after 15 minutes and work once.'}
            </p>
          )}

          <div className="flex flex-col gap-2">
            <label
              htmlFor={CODE_INPUT_ID}
              className="text-[1rem] font-semibold uppercase tracking-[0.2em] text-[#8b98a9]"
            >
              Setup code
            </label>
            <input
              id={CODE_INPUT_ID}
              type="text"
              inputMode="text"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              value={value}
              onChange={handleChange}
              placeholder="XXXX-XXXX"
              // No maxLength: it would browser-truncate a multi-separator
              // paste ("AB-CD-12-EF") before handleChange normalizes; the
              // handler already enforces the 8-char cap.
              className="w-full border border-[#243042] bg-[#070a0f] px-6 py-5 text-center font-mono text-[3rem] font-bold uppercase tracking-[0.35em] tabular-nums text-[#f0f4f9] placeholder:text-[#3a4658] focus:border-[#1B4D9C] focus:outline-none"
              data-testid="tv-pair-input"
            />
          </div>

          <button
            type="submit"
            disabled={!ready}
            className="w-full border border-[#1B4D9C] bg-[#1B4D9C] px-6 py-5 text-[1.75rem] font-bold uppercase tracking-[0.15em] text-white disabled:cursor-not-allowed disabled:border-[#243042] disabled:bg-[#141b26] disabled:text-[#5b6878]"
            data-testid="tv-pair-connect"
          >
            Connect
          </button>
        </form>
      )}
    </div>
  );
}
