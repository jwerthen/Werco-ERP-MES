/**
 * The one-time reveal of a freshly issued API token.
 *
 * The server returns the JWT exactly once and never stores it, so this is the
 * only moment anyone can copy it. The dialog therefore cannot be dismissed by
 * backdrop or Escape — only by the explicit "I copied it" button — and the
 * caller drops the token from memory when that button is pressed. Nothing here
 * logs, persists or puts the token in a URL.
 */

import React, { useEffect, useId, useState } from 'react';
import { ClipboardDocumentIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Button, Modal, useToast } from '../ui';
import type { ApiTokenIssued } from '../../types/apiToken';
import { formatCentralDateTime } from '../../utils/centralTime';

interface ApiTokenRevealDialogProps {
  issued: ApiTokenIssued | null;
  holderName: string;
  /** `false` when the company's AI-egress switch is off; `null` when unknown. */
  aiEgressAllowed: boolean | null;
  onDone: () => void;
}

export default function ApiTokenRevealDialog({
  issued,
  holderName,
  aiEgressAllowed,
  onDone,
}: ApiTokenRevealDialogProps) {
  const titleId = useId();
  const tokenFieldId = useId();
  const { showToast } = useToast();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setCopied(false);
  }, [issued?.id]);

  const copy = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.token);
      setCopied(true);
    } catch {
      showToast('warning', 'The clipboard is unavailable here. Select the token and copy it by hand.');
    }
  };

  return (
    <Modal
      open={issued !== null}
      onClose={() => undefined}
      closeOnBackdrop={false}
      closeOnEscape={false}
      size="2xl"
      ariaLabelledBy={titleId}
    >
      {issued && (
        <div className="space-y-4" data-testid="api-token-reveal">
          <div>
            <h2 id={titleId} className="text-lg font-bold text-fd-ink">
              API token issued
            </h2>
            <p className="mt-1 text-sm text-fd-mute">
              “{issued.label}” acts as {holderName}.
            </p>
          </div>

          <div
            role="alert"
            className="flex items-start gap-2.5 rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-body"
          >
            <ExclamationTriangleIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-amber" aria-hidden="true" />
            <p>
              <span className="font-semibold text-fd-amber">This token is shown once.</span> It is not stored and cannot
              be retrieved again. Copy it now into the client that will hold it. If it is lost, revoke this token and
              issue another.
            </p>
          </div>

          <div>
            <label htmlFor={tokenFieldId} className="label">
              Token
            </label>
            <textarea
              id={tokenFieldId}
              readOnly
              rows={4}
              value={issued.token}
              spellCheck={false}
              autoComplete="off"
              className="input w-full break-all font-mono text-xs"
              onFocus={event => event.currentTarget.select()}
              data-testid="api-token-value"
            />
            <p className="mt-1 text-[11px] text-fd-mute">
              Send it as <span className="font-mono">Authorization: Bearer &lt;token&gt;</span>.
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-sm border border-fd-line bg-fd-panel p-3 text-xs sm:grid-cols-4">
            <div>
              <dt className="text-fd-mute">Scope</dt>
              <dd className="font-mono text-fd-ink">{issued.scope === 'read_only' ? 'read-only' : 'full'}</dd>
            </div>
            <div>
              <dt className="text-fd-mute">AI-external</dt>
              <dd className="font-mono text-fd-ink">{issued.ai_external ? 'yes' : 'no'}</dd>
            </div>
            <div>
              <dt className="text-fd-mute">JTI prefix</dt>
              <dd className="font-mono text-fd-ink">{issued.jti_prefix}</dd>
            </div>
            <div>
              <dt className="text-fd-mute">Expires</dt>
              <dd className="font-mono text-fd-ink">
                {issued.expires_at ? formatCentralDateTime(issued.expires_at) : 'Never'}
              </dd>
            </div>
          </dl>

          {issued.ai_external && aiEgressAllowed === false && (
            <p className="text-xs text-fd-amber" data-testid="reveal-egress-warning">
              AI egress is off for this company: this token is refused (403) until an Admin enables it under Admin
              Settings → AI Privacy.
            </p>
          )}

          <div className="flex flex-wrap justify-end gap-3 pt-1">
            <Button variant="secondary" onClick={copy}>
              <ClipboardDocumentIcon className="mr-1 h-4 w-4" aria-hidden="true" />
              {copied ? 'Copied' : 'Copy token'}
            </Button>
            <Button onClick={onDone}>Done — I copied it</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
