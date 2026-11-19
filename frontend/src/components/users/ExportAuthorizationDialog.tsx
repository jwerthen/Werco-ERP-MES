import React, { useEffect, useId, useState } from 'react';
import { ShieldCheckIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { FormField, LoadingButton, Modal, useToast } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDateTime } from '../../utils/centralTime';

/** The user fields the export-authorization UI reads (all on UserResponse). */
export interface ExportAuthorizationUser {
  id: number;
  first_name: string;
  last_name: string;
  employee_id?: string | null;
  export_authorized?: boolean;
  export_authorized_at?: string | null;
  export_authorized_by?: number | null;
}

/** Resolves the granting user's id to a display name; the caller knows the user list. */
export type GrantorName = (userId: number) => string | undefined;

/**
 * Who granted the authorization and when (Central time), as far as the record says. A grantor
 * outside the loaded list (inactive, or a platform principal) reads as their user id.
 */
export function exportAuthorizationGrant(
  user: ExportAuthorizationUser,
  grantorName?: GrantorName
): { by: string | null; at: string | null } {
  const grantor = user.export_authorized_by;
  return {
    by: grantor == null ? null : (grantorName?.(grantor) ?? `user #${grantor}`),
    at: user.export_authorized_at ? formatCentralDateTime(user.export_authorized_at) : null,
  };
}

/**
 * The Users-table cell: whether the person is export-authorized, who granted it and when, and
 * the one action that opens `ExportAuthorizationDialog`. Admin only; the caller gates it.
 */
export function ExportAuthorizationCell({
  user,
  grantorName,
  onManage,
}: {
  user: ExportAuthorizationUser;
  grantorName?: GrantorName;
  onManage: (user: ExportAuthorizationUser) => void;
}) {
  const name = `${user.first_name} ${user.last_name}`;
  const verb = user.export_authorized ? 'Revoke' : 'Grant';
  const { by, at } = exportAuthorizationGrant(user, grantorName);
  const grant = [by ? `by ${by}` : null, at].filter(Boolean).join(' · ');
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-0 text-sm">
        {user.export_authorized ? (
          <>
            <span className="font-medium text-emerald-300">Authorized</span>
            {grant && <span className="block text-xs text-slate-400">{grant}</span>}
          </>
        ) : (
          <span className="text-slate-400">Not authorized</span>
        )}
      </div>
      <button
        type="button"
        onClick={() => onManage(user)}
        className="ml-auto shrink-0 rounded-[3px] border border-slate-600 px-2 py-1 text-xs font-medium text-slate-200 hover:border-blue-400 hover:text-blue-300"
        aria-label={`${verb} export authorization for ${name}`}
      >
        {verb}
      </button>
    </div>
  );
}

interface ExportAuthorizationDialogProps {
  /** The user being granted or revoked; null = closed. */
  user: ExportAuthorizationUser | null;
  grantorName?: GrantorName;
  onClose: () => void;
  /** Called after the server accepted the change — reload the list. */
  onChanged: () => void | Promise<void>;
}

/**
 * Users → Export authorization (AS9100D 7.5.3.1). Admin only
 * (PUT /users/{id}/export-authorization), never through an API token.
 *
 * Export authorization is the ONLY way to open an export-controlled document: no role bypasses
 * it, Admin included. Granting or revoking needs a reason, which lands on the audit row.
 * Non-optimistic: the server's refusal is shown verbatim, and the list reflects only what the
 * reload returns.
 */
export default function ExportAuthorizationDialog({
  user,
  grantorName,
  onClose,
  onChanged,
}: ExportAuthorizationDialogProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // A fresh, empty form every time the dialog opens (or switches person).
  useEffect(() => {
    setReason('');
    setReasonError(null);
    setServerError(null);
  }, [user?.id]);

  if (!user) return null;

  const name = `${user.first_name} ${user.last_name}`;
  const authorized = !!user.export_authorized;
  const grant = exportAuthorizationGrant(user, grantorName);

  const close = () => {
    if (!saving) onClose();
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const trimmed = reason.trim();
    if (!trimmed) {
      setReasonError('Give a reason. It is recorded on the audit trail.');
      return;
    }
    setReasonError(null);
    setSaving(true);
    setServerError(null);
    try {
      await api.setUserExportAuthorization(user.id, { export_authorized: !authorized, reason: trimmed });
      showToast(
        'success',
        authorized ? `Export authorization revoked for ${name}` : `${name} is now export-authorized`
      );
      await onChanged();
      onClose();
    } catch (err) {
      // 403 is a role / credential refusal; 409 a user this caller may not manage.
      setServerError(apiErrorDetail(err, 'Could not change export authorization.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={close} size="md" ariaLabelledBy={titleId} closeOnBackdrop={false} closeOnEscape={!saving}>
      <h3 id={titleId} className="mb-1 flex items-center gap-2 text-lg font-semibold">
        <ShieldCheckIcon className="h-5 w-5 text-slate-300" aria-hidden="true" />
        Export authorization — {name}
      </h3>
      <p className="mb-4 text-sm text-slate-400">
        Only export-authorized people can open a document marked export-controlled, on every screen including the kiosk.
        No role overrides this, Admin included.
      </p>

      <dl className="mb-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-[4px] border border-slate-700 px-3 py-2.5 text-sm">
        {user.employee_id ? (
          <>
            <dt className="text-slate-400">Badge / ID</dt>
            <dd className="font-mono text-slate-200">{user.employee_id}</dd>
          </>
        ) : null}
        <dt className="text-slate-400">Status</dt>
        <dd className={authorized ? 'font-medium text-emerald-300' : 'text-slate-200'}>
          {authorized ? 'Export-authorized' : 'Not authorized'}
        </dd>
        {authorized && grant.by && (
          <>
            <dt className="text-slate-400">Granted by</dt>
            <dd className="text-slate-200">{grant.by}</dd>
          </>
        )}
        {authorized && grant.at && (
          <>
            <dt className="text-slate-400">Granted</dt>
            <dd className="text-slate-200">{grant.at}</dd>
          </>
        )}
      </dl>

      {serverError && (
        <div role="alert" className="alert alert-danger mb-4 text-sm">
          <span>{serverError}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <FormField
          label={authorized ? 'Reason for revoking' : 'Reason for granting'}
          required
          error={reasonError}
          help="Recorded on the audit trail."
        >
          {field => (
            <textarea
              {...field}
              rows={3}
              maxLength={500}
              value={reason}
              onChange={e => setReason(e.target.value)}
              className={reasonError ? 'input-error' : 'input'}
              disabled={saving}
            />
          )}
        </FormField>
        {authorized && (
          <p className="text-xs text-slate-400">
            {name} loses access to every export-controlled document as soon as this is saved.
          </p>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2 pt-2">
          <button type="button" onClick={close} disabled={saving} className="btn-secondary">
            Cancel
          </button>
          <LoadingButton
            type="submit"
            loading={saving}
            loadingText="Saving…"
            variant={authorized ? 'danger' : 'primary'}
          >
            {authorized ? 'Revoke authorization' : 'Grant authorization'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
