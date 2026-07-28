/**
 * Automatic BOM/routing backflush — the opt-in card on a part's detail page.
 *
 * `Part.backflush_components` is a shop-wide policy switch, not a preference.
 * With it on, COMPLETING a work order for this part pulls the part's BOM (and
 * routing-named) components out of stock automatically, forever after, and
 * writes the lots it drew onto the as-built genealogy record. Nothing here asks
 * a human before each draw — the flip IS the consent — so the card's whole job
 * is to make that plain and to show what the server thinks of this part's BOM
 * before anyone flips it.
 *
 * Four things shape every decision below:
 *
 * 1. **The write is SERVER-GATED, so this is strictly NON-OPTIMISTIC.** Enabling
 *    is refused **409** while any blocking readiness diagnostic stands, with a
 *    plain-string `detail` — one sentence per blocker. The card keeps a loading
 *    state, renders only what the server returns, and shows that `detail`
 *    verbatim. It never paints the new state and rolls back.
 *
 * 2. **`eligible` is a snapshot, never authorisation.** BOM lines, alternates
 *    and routing component ids are all mutable by other people between this
 *    read and the write, so the identical check re-runs server-side. That is
 *    why the confirm button is NOT disabled on a known-blocked part: a dead
 *    button says nothing about why, and the authoritative answer is the one the
 *    server gives. (Same call `MaterialTiesPanel` makes.)
 *
 * 3. **Only the BOM half is answerable here.** Routing conditions — an
 *    operation naming the work order's own part, two operations disagreeing on
 *    a component, routing demand the BOM excludes — need a work order to
 *    resolve against and surface on that work order's backflush preview
 *    instead. The card says so rather than implying a clean part is clean
 *    everywhere.
 *
 * 4. **Optimistic locking on parts is COSMETIC.** `Part` maps no `version`
 *    column server-side, so a concurrent flip does not 409 — last write wins.
 *    The card therefore adopts the part object the server hands back rather than
 *    a locally toggled copy, and re-reads readiness afterwards.
 *
 * Deliberately NOT wired through `PartOverviewTab`'s inline `EditableField`:
 * that PUTs `{field, version}` through `updatePart`, whose client type does not
 * carry the flag (mirroring the server, where it is absent from
 * `PartBase`/`PartCreate` so no create path or CSV importer can set it).
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowPathRoundedSquareIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, ErrorState, LoadingButton, Modal, Skeleton, useToast } from '../ui';
import { toDisplayString } from '../../utils/apiError';
import type { BackflushDiagnostic, Part, PartBackflushReadiness } from '../../types';

/**
 * Part types that can carry a BOM, and therefore the only ones for which this
 * card is meaningful.
 *
 * A purchased part, raw material, hardware item or consumable will never have a
 * BOM, so `backflush_readiness_for_part` always answers with the
 * `no_demand_source` blocker — and an unconditional card would paint a permanent
 * red "Cannot be enabled — 1 problem to fix first" panel on every one of those
 * pages. That is how an operator learns to ignore red panels on part detail,
 * which costs the real ones their meaning.
 *
 * The flag itself is checked too, so a part that somehow HAS it on (the
 * `seed_data.py` splat is the one path that bypasses the refusal gate) can still
 * be turned off from the UI rather than being stranded on.
 */
export function showsBackflushCard(part: Pick<Part, 'part_type' | 'backflush_components'>): boolean {
  return (
    Boolean(part.backflush_components) || part.part_type === 'manufactured' || part.part_type === 'assembly'
  );
}

export interface PartBackflushCardProps {
  part: Part;
  /** `parts:edit` — ADMIN / MANAGER / SUPERVISOR, matching the server's gate. */
  canEdit: boolean;
  /** Hand the server's own updated part back up; never a locally toggled copy. */
  onPartUpdated: (part: Part) => void;
}

/** Pull a displayable `detail` off any error shape, incl. a structured 409 body. */
function backflushErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const rendered = toDisplayString(detail);
  if (rendered.trim()) return rendered;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

/**
 * One diagnostic, rendered from its `detail` sentence — never from a prettified
 * `code`. The server writes `detail` to read correctly inside its own refusal
 * message, so showing anything else would let this card drift from the 409 the
 * user is about to get.
 */
function DiagnosticRow({ diagnostic, tone }: { diagnostic: BackflushDiagnostic; tone: 'blocking' | 'advisory' }) {
  const blocking = tone === 'blocking';
  return (
    <li
      className={`flex items-start gap-2 rounded-sm border px-2.5 py-2 text-xs ${
        blocking ? 'border-fd-red/40 bg-fd-red/10 text-red-200' : 'border-fd-amber/35 bg-fd-amber/10 text-amber-200'
      }`}
    >
      <ExclamationTriangleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block">{diagnostic.detail}</span>
        <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wider opacity-60">
          {diagnostic.code}
          {diagnostic.component_part_number ? ` · ${diagnostic.component_part_number}` : ''}
        </span>
      </span>
    </li>
  );
}

export function PartBackflushCard({ part, canEdit, onPartUpdated }: PartBackflushCardProps) {
  const { showToast } = useToast();
  const [readiness, setReadiness] = useState<PartBackflushReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  // `null` = the confirm dialog is closed; otherwise the direction being confirmed.
  const [confirmEnable, setConfirmEnable] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  const partId = part.id;

  const loadReadiness = useCallback(async () => {
    setLoadError('');
    try {
      // Pure read — writes nothing, so it is safe on render and safe to re-run.
      setReadiness(await api.getPartBackflushReadiness(partId));
    } catch (err) {
      setReadiness(null);
      setLoadError(backflushErrorDetail(err, 'Could not read this part’s backflush readiness'));
    } finally {
      setLoading(false);
    }
  }, [partId]);

  useEffect(() => {
    setLoading(true);
    void loadReadiness();
  }, [loadReadiness]);

  const enabled = Boolean(part.backflush_components);
  const blockers = readiness?.blockers ?? [];
  const advisories = readiness?.advisories ?? [];

  const closeConfirm = () => {
    // Never dismiss mid-request: this write changes what a completion does to
    // stock, and the card must reflect only what the server actually did.
    if (saving) return;
    setConfirmEnable(null);
    setSaveError('');
  };

  const handleConfirm = async () => {
    if (confirmEnable === null || saving) return;
    const target = confirmEnable;
    setSaving(true);
    setSaveError('');
    try {
      // Non-optimistic by contract: nothing in the UI moves until this resolves,
      // and what it resolves WITH is what we adopt.
      const updated = await api.setPartBackflush(partId, part.version, target);
      onPartUpdated(updated);
      showToast(
        'success',
        target
          ? `${updated.part_number} now backflushes its BOM components automatically at work-order completion`
          : `${updated.part_number} no longer backflushes its BOM components`
      );
      setConfirmEnable(null);
      // The flag is echoed on the readiness read too; re-run it so the card and
      // the server cannot disagree about state.
      await loadReadiness();
    } catch (err) {
      // The 409 is a plain string: one sentence per blocker, naming the BOM line
      // to fix. It is the whole point of the refusal, so it renders in full.
      setSaveError(backflushErrorDetail(err, 'Failed to change automatic backflush for this part'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card card-compact" data-testid="part-backflush-card">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="card-title flex items-center gap-2">
            <ArrowPathRoundedSquareIcon className="h-5 w-5 flex-shrink-0 text-slate-400" aria-hidden="true" />
            Automatic BOM Backflush
          </h2>
          <p className="card-subtitle">
            With this on, completing a work order for this part takes its BOM components out of stock
            automatically and records the lots it drew. Nothing asks first — this switch is the consent.
          </p>
        </div>
        <span
          data-testid="part-backflush-state"
          className={`shrink-0 self-start whitespace-nowrap rounded-sm border px-2 py-0.5 font-mono text-xs uppercase tracking-wider ${
            enabled ? 'border-fd-green/45 bg-fd-green/10 text-fd-green' : 'border-fd-line text-slate-400'
          }`}
        >
          {enabled ? 'On' : 'Off'}
        </span>
      </div>

      {loading ? (
        <Skeleton className="h-16 w-full" />
      ) : loadError ? (
        <ErrorState
          message={loadError}
          onRetry={() => {
            setLoading(true);
            void loadReadiness();
          }}
        />
      ) : (
        <div className="space-y-3">
          {blockers.length > 0 ? (
            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-red">
                {enabled
                  ? `${blockers.length} problem${blockers.length === 1 ? '' : 's'} with this part’s BOM`
                  : `Cannot be enabled — ${blockers.length} problem${blockers.length === 1 ? '' : 's'} to fix first`}
              </p>
              <ul className="space-y-1.5" data-testid="part-backflush-blockers">
                {/* Index-keyed on purpose: two diagnostics can legitimately share
                    a code with no row context (e.g. two unresolvable BOM lines),
                    so code alone is not a unique key. */}
                {blockers.map((diagnostic, index) => (
                  <DiagnosticRow key={`${diagnostic.code}-${index}`} diagnostic={diagnostic} tone="blocking" />
                ))}
              </ul>
              {enabled && (
                <p className="mt-1.5 text-xs text-slate-500">
                  Backflush is already on for this part, so these are not blocking anything today — they describe
                  what the next completion would resolve wrongly. Turning it off is always allowed.
                </p>
              )}
            </div>
          ) : (
            <div className="inline-flex items-start gap-1.5 rounded-sm border border-fd-green/30 bg-fd-green/10 px-2 py-1.5 text-xs text-fd-green">
              <CheckCircleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              <span>
                This part’s BOM resolves cleanly. Nothing blocks automatic backflush.
              </span>
            </div>
          )}

          {advisories.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-amber">
                Worth a look — {advisories.length} advisor{advisories.length === 1 ? 'y' : 'ies'} (not blocking)
              </p>
              <ul className="space-y-1.5" data-testid="part-backflush-advisories">
                {advisories.map((diagnostic, index) => (
                  <DiagnosticRow key={`${diagnostic.code}-${index}`} diagnostic={diagnostic} tone="advisory" />
                ))}
              </ul>
            </div>
          )}

          {/* Fact 3: what this check genuinely cannot see. Stated rather than
              implied, so a clean verdict here is not read as clean everywhere. */}
          <p className="flex items-start gap-1.5 text-xs text-slate-500">
            <InformationCircleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
            <span>
              This checks the BOM only. Routing-level problems — an operation naming this part as its own
              component, two operations disagreeing on a quantity, routing demand the BOM excludes — need a work
              order to resolve against and show up on its{' '}
              <Link to="/work-orders" className="text-fd-cyan underline-offset-2 hover:underline">
                backflush preview
              </Link>
              , alongside the exact lots a completion would draw.
            </span>
          </p>

          {canEdit && (
            <div className="flex flex-wrap items-center gap-2 border-t border-fd-line pt-3">
              <Button
                variant={enabled ? 'secondary' : 'primary'}
                size="sm"
                onClick={() => {
                  setConfirmEnable(!enabled);
                  setSaveError('');
                }}
              >
                {enabled ? 'Turn off automatic backflush' : 'Turn on automatic backflush'}
              </Button>
              <span className="text-xs text-slate-500">
                Recorded on the audit trail with your name.
              </span>
            </div>
          )}
        </div>
      )}

      {/* --- Confirm (server-GATED, NON-optimistic) ------------------------- */}
      <Modal open={confirmEnable !== null} onClose={closeConfirm} size="lg" padded={false} scroll={false}>
        {confirmEnable !== null && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">
                {confirmEnable ? 'Turn on automatic backflush' : 'Turn off automatic backflush'} — {part.part_number}
              </h3>
            </div>
            <div className="modal-body max-h-[70vh] space-y-3 overflow-y-auto">
              {confirmEnable ? (
                <>
                  <div className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200/90">
                    From now on, every work order for {part.part_number} that completes will take this part’s BOM
                    components out of stock by itself, and the lots it draws are written onto the as-built record.
                    Nothing asks first, and consumption never reverses on its own — putting material back is a
                    separate, reasoned correction.
                  </div>
                  <p className="text-sm text-slate-300">
                    It applies to future completions. It does not reach back and consume for work orders that have
                    already closed.
                  </p>
                </>
              ) : (
                <p className="text-sm text-slate-300">
                  Completions of {part.part_number} will stop taking its BOM components out of stock. Material
                  already consumed stays consumed — this changes nothing that has already posted, and it is always
                  allowed, even while the BOM has problems.
                </p>
              )}

              {confirmEnable && blockers.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-red">
                    The server will refuse this while these stand
                  </p>
                  <ul className="space-y-1.5">
                    {blockers.map((diagnostic, index) => (
                      <DiagnosticRow
                        key={`confirm-${diagnostic.code}-${index}`}
                        diagnostic={diagnostic}
                        tone="blocking"
                      />
                    ))}
                  </ul>
                </div>
              )}

              {confirmEnable && (
                <p className="text-xs text-slate-500">
                  The BOM is re-checked on the server as this is saved — this list was read a moment ago and is not
                  permission. Parts carry no working optimistic lock, so a simultaneous edit by someone else does
                  not conflict; last write wins.
                </p>
              )}

              {/* Verbatim server refusal — the primary display for a gated write.
                  The 409 names the BOM line to fix, which is the entire reason
                  it renders in full rather than as a generic failure. */}
              {saveError && (
                <div
                  role="alert"
                  data-testid="part-backflush-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
                >
                  {saveError}
                </div>
              )}
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={closeConfirm} disabled={saving}>
                Cancel
              </Button>
              {/* Deliberately NOT disabled on a known-blocked part — the same
                  call MaterialTiesPanel makes. A dead button says nothing about
                  why; the server's own sentence does. */}
              <LoadingButton
                variant={confirmEnable ? 'primary' : 'danger'}
                loading={saving}
                loadingText="Saving…"
                onClick={handleConfirm}
              >
                {confirmEnable ? 'Turn it on' : 'Turn it off'}
              </LoadingButton>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}

export default PartBackflushCard;
