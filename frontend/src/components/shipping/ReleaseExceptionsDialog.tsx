import React, { useCallback, useEffect, useId, useState } from 'react';
import api from '../../services/api';
import { Button, ErrorState, FormField, LoadingButton, Modal } from '../ui';
import { toDisplayString } from '../../utils/apiError';
import {
  releaseExceptionsConflict,
  type CertificateOfConformance,
  type MarkShippedResult,
  type ReleaseException,
} from '../../types/shipping';
import { ReleaseExceptionList } from './ReleaseExceptionChips';

/** Mirrors ``shipment_release_service.RELEASE_ACK_REASON_MIN_LENGTH``. */
export const RELEASE_ACK_REASON_MIN_LENGTH = 3;
const RELEASE_ACK_REASON_MAX_LENGTH = 1000;

export interface ReleaseExceptionsDialogTarget {
  id: number;
  shipment_number: string;
  work_order_number?: string | null;
}

interface ReleaseExceptionsDialogProps {
  /** ``ship`` = POST /shipping/{id}/ship; ``coc`` = POST /shipping/{id}/coc. */
  mode: 'ship' | 'coc';
  target: ReleaseExceptionsDialogTarget;
  /** Admin, Manager or Quality (the server's RELEASE_ACK_ROLES). */
  canAccept: boolean;
  /** Quality without a shipping role: ships only to accept open items. */
  qualityOnly?: boolean;
  onClose: () => void;
  onShipped?: (result: MarkShippedResult) => void;
  onIssued?: (certificate: CertificateOfConformance) => void;
}

/**
 * The Ship and Issue CoC confirmation, with the job's open quality items.
 *
 * Server-GATED, so NON-optimistic: it reads GET /shipping/{id} for the open items, and
 * the confirm shows only what the server answers. Nothing open: a plain confirm that
 * sends no body. Something open: the list, and for Admin, Manager or Quality an
 * acknowledgment checkbox plus a required reason, sent with the request; anyone else is
 * told who must accept. A 409 that arrives anyway (an item opened since the read)
 * replaces the list from the server's own detail and asks for the acknowledgment again.
 * While the request is in flight nothing can close the dialog. Mount only while open.
 */
export default function ReleaseExceptionsDialog({
  mode,
  target,
  canAccept,
  qualityOnly = false,
  onClose,
  onShipped,
  onIssued,
}: ReleaseExceptionsDialogProps) {
  const titleId = useId();
  const [exceptions, setExceptions] = useState<ReleaseException[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [acknowledged, setAcknowledged] = useState(false);
  const [reason, setReason] = useState('');
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setExceptions(null);
    setLoadError(false);
    api
      .getShipment(target.id)
      .then(detail => {
        if (!cancelled) setExceptions(detail.release_exceptions ?? []);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [target.id, attempt]);

  const hasOpenItems = (exceptions?.length ?? 0) > 0;
  const qualityHasNothingToAccept = mode === 'ship' && qualityOnly && exceptions !== null && !hasOpenItems;
  const blockedForRole = hasOpenItems && !canAccept;
  const trimmedReason = reason.trim();
  const canConfirm =
    exceptions !== null &&
    !pending &&
    !blockedForRole &&
    !qualityHasNothingToAccept &&
    (!hasOpenItems || (acknowledged && trimmedReason.length >= RELEASE_ACK_REASON_MIN_LENGTH));

  const close = useCallback(() => {
    if (!pending) onClose();
  }, [pending, onClose]);

  const submit = async () => {
    if (!canConfirm) return;
    setPending(true);
    setSubmitError(null);
    const acknowledgment = hasOpenItems ? { acknowledge_exceptions: true, reason: trimmedReason } : undefined;
    try {
      if (mode === 'ship') {
        const result = await api.markShipped(target.id, undefined, acknowledgment);
        onShipped?.(result);
      } else {
        const certificate = await api.issueCoc(target.id, acknowledgment);
        onIssued?.(certificate);
      }
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail;
      const conflict = releaseExceptionsConflict(detail);
      if (conflict) {
        setExceptions(conflict.release_exceptions);
        setAcknowledged(false);
        setSubmitError(conflict.message);
      } else {
        setSubmitError(
          toDisplayString(detail) ||
            (mode === 'ship' ? 'Failed to mark the shipment shipped.' : 'Failed to issue the certificate.')
        );
      }
    } finally {
      setPending(false);
    }
  };

  const title =
    mode === 'ship'
      ? `Ship ${target.shipment_number}`
      : `Issue Certificate of Conformance for ${target.shipment_number}`;
  const confirmLabel =
    mode === 'ship'
      ? hasOpenItems
        ? 'Accept and ship'
        : 'Mark shipped'
      : hasOpenItems
        ? 'Accept and issue'
        : 'Issue CoC';

  return (
    <Modal open onClose={close} size="lg" closeOnBackdrop={!pending} closeOnEscape={!pending} ariaLabelledBy={titleId}>
      <div className="space-y-4">
        <div>
          <h2 id={titleId} className="text-lg font-semibold text-fd-ink">
            {title}
          </h2>
          {target.work_order_number && <p className="text-sm text-fd-mute">Work order {target.work_order_number}</p>}
        </div>

        {loadError ? (
          <ErrorState
            title="Could not load the shipment"
            message="The open quality items could not be read. Try again."
            onRetry={() => setAttempt(value => value + 1)}
          />
        ) : exceptions === null ? (
          <div role="status" className="py-6 text-center text-sm text-fd-mute">
            Checking open quality items…
          </div>
        ) : (
          <>
            {hasOpenItems ? (
              <div className="space-y-2">
                <p className="text-sm font-medium text-amber-300">
                  This job has {exceptions.length} open quality item{exceptions.length === 1 ? '' : 's'}.
                </p>
                <ReleaseExceptionList exceptions={exceptions} />
                {mode === 'ship' && (
                  <p className="text-sm text-fd-mute">
                    Shipping over them withholds the automatic Certificate of Conformance; Quality issues it.
                  </p>
                )}
              </div>
            ) : (
              <p className="text-sm text-fd-mute">
                {mode === 'ship'
                  ? 'Nothing is open on this job. Marking it shipped closes the work order once it is fully shipped.'
                  : 'Nothing is open on this job. The numbered certificate is issued under your name.'}
              </p>
            )}

            {qualityHasNothingToAccept && (
              <p role="note" className="text-sm text-fd-mute">
                Quality marks a shipment shipped only to accept open quality items on it. Shipping, Supervisor, Manager
                or Admin ships a job with nothing open.
              </p>
            )}

            {blockedForRole && (
              <p role="note" className="text-sm text-amber-300">
                Admin, Manager or Quality must accept these items with a reason
                {mode === 'ship' ? ' to ship.' : ' to issue the certificate.'}
              </p>
            )}

            {hasOpenItems && canAccept && (
              <div className="space-y-3 border-t border-fd-line pt-3">
                <label className="flex items-start gap-2 text-sm text-fd-ink">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={acknowledged}
                    disabled={pending}
                    onChange={e => setAcknowledged(e.target.checked)}
                  />
                  <span>
                    I accept these open quality items
                    {mode === 'ship' ? ' and ship this shipment over them.' : ' on the certificate.'}
                  </span>
                </label>
                <FormField
                  label="Reason"
                  required
                  help={`At least ${RELEASE_ACK_REASON_MIN_LENGTH} characters. Recorded with your name in the audit log.`}
                >
                  {field => (
                    <textarea
                      {...field}
                      className="input"
                      rows={3}
                      maxLength={RELEASE_ACK_REASON_MAX_LENGTH}
                      value={reason}
                      disabled={pending}
                      onChange={e => setReason(e.target.value)}
                    />
                  )}
                </FormField>
              </div>
            )}
          </>
        )}

        {submitError && (
          <p role="alert" className="text-sm text-red-300">
            {submitError}
          </p>
        )}

        <div className="flex flex-wrap justify-end gap-3 border-t border-fd-line pt-3">
          <Button variant="secondary" disabled={pending} onClick={close}>
            Cancel
          </Button>
          <LoadingButton loading={pending} disabled={!canConfirm} onClick={submit}>
            {confirmLabel}
          </LoadingButton>
        </div>
      </div>
    </Modal>
  );
}
