/**
 * Purchasing → Quality flow-down (AS9100D 8.4.3): the company's supplier-quality flow-down
 * profile and its quality-clause catalog. Mounted only for ADMIN / MANAGER / QUALITY (the
 * server's role set on every write here).
 *
 * What a purchase order prints: the profile's heading, document reference and standard
 * paragraph, then each clause the PO selected (code — title, clause text under it). A DRAFT
 * prints the live settings; issuing the PO freezes the block onto it, so an edit here never
 * changes a PO that has already gone to a supplier.
 *
 * Every write is server-GATED, so non-optimistic: the screen reflects only what the server
 * returned. The profile carries a version — a stale save is a 409, whose detail is shown and
 * after which the profile is re-read so the next save starts from what is actually stored.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { PlusIcon, ShieldCheckIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, ErrorState, FormField, LoadingButton, StatusBadge, useToast } from '../ui';
import type { DataTableColumn } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { POFlowdownProfile, POQualityClause } from '../../types/poFlowdown';
import {
  flowdownProfileSchema,
  type FlowdownProfileFormData,
  type FlowdownProfileFormInput,
} from '../../validation/poFlowdown';
import { flowdownErrorDetail, flowdownErrorStatus, sortQualityClauses } from '../../utils/qualityFlowdown';
import { QualityClauseDialog } from './QualityClauseDialog';

const DEFAULT_HEADING = 'Supplier Quality Requirements';

function formFromProfile(profile: POFlowdownProfile | null): FlowdownProfileFormInput {
  return {
    enabled: profile?.enabled ?? false,
    heading: profile?.heading ?? DEFAULT_HEADING,
    document_reference: profile?.document_reference ?? '',
    body: profile?.body ?? '',
  };
}

function FlowdownProfileForm() {
  const { showToast } = useToast();
  const [profile, setProfile] = useState<POFlowdownProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  // The server's 409 text, kept on screen after the reload so the user knows why their
  // edit is gone.
  const [conflict, setConflict] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<FlowdownProfileFormInput, unknown, FlowdownProfileFormData>({
    resolver: zodResolver(flowdownProfileSchema),
    defaultValues: formFromProfile(null),
  });

  const { markSaved } = useUnsavedChanges(isDirty);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      setProfile(await api.getPOFlowdownProfile());
    } catch (err) {
      console.error('Failed to load the flow-down profile:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The form follows the stored profile: every load, save and 409 re-read sets `profile`, and
  // the form is reset from it HERE, never inside the submit handler. A reset() run from within
  // handleSubmit leaves react-hook-form's dirty tracking dead for the next edit (measured: Save
  // stayed disabled after typing).
  useEffect(() => {
    if (profile) reset(formFromProfile(profile));
  }, [profile, reset]);

  const onSubmit = async (data: FlowdownProfileFormData) => {
    if (saving || !profile) return;
    setSaving(true);
    setConflict(null);
    try {
      const saved = await api.updatePOFlowdownProfile({
        enabled: data.enabled,
        heading: data.heading,
        document_reference: data.document_reference || null,
        body: data.body.trim() ? data.body.trim() : null,
        version: profile.version,
      });
      setProfile(saved);
      markSaved();
      showToast('success', 'Flow-down profile saved. Draft purchase orders print it from now on.');
    } catch (err) {
      const detail = flowdownErrorDetail(err, 'Could not save the flow-down profile. Nothing was changed.');
      if (flowdownErrorStatus(err) === 409) {
        // Someone saved since this form was loaded. Show why, then re-read what is stored.
        setConflict(detail);
        showToast('error', detail);
        await load();
      } else {
        showToast('error', detail);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-labelledby="flowdown-profile-heading" className="space-y-4">
      <div>
        <h2 id="flowdown-profile-heading" className="text-lg font-semibold">
          Flow-down profile
        </h2>
        <p className="mt-1 text-sm text-fd-mute">
          The supplier quality requirements block printed on every purchase order, before the notes. A draft prints the
          current settings; issuing a purchase order freezes them onto it.
        </p>
      </div>

      {loading && !profile ? (
        <p role="status" className="text-sm text-fd-mute">
          Loading flow-down profile…
        </p>
      ) : loadError && !profile ? (
        <ErrorState message="Could not load the flow-down profile." onRetry={() => void load()} />
      ) : (
        <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
          {conflict && (
            <div
              role="alert"
              className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
            >
              {conflict} The profile was reloaded with the saved version; make your change again.
            </div>
          )}
          {/* A disabled FIELDSET, not per-input `disabled`: react-hook-form reads a natively
              disabled input as undefined, while a fieldset leaves each input's own property
              alone. */}
          <fieldset disabled={saving} className="space-y-4">
            <div className="flex items-center gap-2">
              <input
                id="flowdown-profile-enabled"
                type="checkbox"
                className="rounded border-slate-600"
                {...register('enabled')}
              />
              <label htmlFor="flowdown-profile-enabled" className="text-sm text-fd-body">
                Print the flow-down block on purchase orders
              </label>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <FormField label="Heading" required error={errors.heading?.message} className="sm:col-span-2">
                {field => (
                  <input
                    {...field}
                    {...register('heading')}
                    type="text"
                    maxLength={255}
                    className={errors.heading ? 'input input-error w-full' : 'input w-full'}
                  />
                )}
              </FormField>
              <FormField
                label="Document reference"
                error={errors.document_reference?.message}
                help='Printed as "Reference: …". Blank prints no reference line.'
              >
                {field => (
                  <input
                    {...field}
                    {...register('document_reference')}
                    type="text"
                    maxLength={100}
                    className={errors.document_reference ? 'input input-error w-full' : 'input w-full'}
                  />
                )}
              </FormField>
            </div>
            <FormField
              label="Standard flow-down text"
              error={errors.body?.message}
              help="Printed under the heading. Separate paragraphs with a blank line."
            >
              {field => <textarea {...field} {...register('body')} rows={6} className="input w-full" />}
            </FormField>
          </fieldset>
          <div className="flex items-center justify-end gap-3">
            {profile && !profile.id && (
              <span className="mr-auto text-xs text-fd-mute">Not saved yet — these are the defaults.</span>
            )}
            <Button
              variant="secondary"
              onClick={() => {
                reset(formFromProfile(profile));
                setConflict(null);
              }}
              disabled={saving || !isDirty}
            >
              Discard changes
            </Button>
            <LoadingButton type="submit" loading={saving} loadingText="Saving…" disabled={!isDirty && !!profile?.id}>
              Save profile
            </LoadingButton>
          </div>
        </form>
      )}
    </section>
  );
}

function QualityClauseCatalog() {
  const { showToast } = useToast();
  const [clauses, setClauses] = useState<POQualityClause[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  // `undefined` = dialog closed, `null` = adding, a clause = editing it.
  const [editing, setEditing] = useState<POQualityClause | null | undefined>(undefined);
  const [togglePendingId, setTogglePendingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const rows = await api.getPOQualityClauses({ include_inactive: true });
      setClauses(sortQualityClauses(Array.isArray(rows) ? rows : []));
    } catch (err) {
      console.error('Failed to load quality clauses:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const upsert = (saved: POQualityClause) =>
    setClauses(prev => sortQualityClauses([...prev.filter(clause => clause.id !== saved.id), saved]));

  const nextSortOrder = clauses.reduce((max, clause) => Math.max(max, clause.sort_order), 0) + 10;

  // Retire / reactivate. Non-optimistic: the row changes only when the server says so.
  const toggleActive = async (clause: POQualityClause) => {
    if (togglePendingId !== null) return;
    setTogglePendingId(clause.id);
    try {
      // The PUT is partial: send the one flag, so a concurrent title/text edit survives.
      const saved = await api.updatePOQualityClause(clause.id, { is_active: !clause.is_active });
      upsert(saved);
      showToast(
        'success',
        saved.is_active
          ? `Clause ${saved.code} reactivated. It is offered on purchase orders again.`
          : `Clause ${saved.code} retired. It is no longer offered or printed; issued purchase orders keep their frozen copy.`
      );
    } catch (err) {
      showToast('error', flowdownErrorDetail(err, `Could not update clause ${clause.code}. Nothing was changed.`));
    } finally {
      setTogglePendingId(null);
    }
  };

  const columns: Array<DataTableColumn<POQualityClause>> = [
    {
      key: 'code',
      header: 'Code',
      sortable: true,
      accessor: clause => clause.code,
      render: clause => <span className="font-mono font-medium text-werco-primary">{clause.code}</span>,
    },
    {
      key: 'title',
      header: 'Title',
      sortable: true,
      accessor: clause => clause.title,
      render: clause => (
        <div className="max-w-xl">
          <div>{clause.title}</div>
          {clause.body && <div className="text-xs text-fd-mute line-clamp-2">{clause.body}</div>}
        </div>
      ),
      csv: clause => clause.title,
    },
    {
      key: 'default_selected',
      header: 'Default',
      align: 'center',
      sortable: true,
      accessor: clause => (clause.default_selected ? 1 : 0),
      render: clause => (clause.default_selected ? 'Yes' : <span className="text-fd-mute">—</span>),
      csv: clause => (clause.default_selected ? 'Yes' : 'No'),
    },
    {
      key: 'is_active',
      header: 'Status',
      sortable: true,
      accessor: clause => (clause.is_active ? 'active' : 'retired'),
      render: clause => <StatusBadge status={clause.is_active ? 'active' : 'retired'} />,
    },
    {
      key: 'sort_order',
      header: 'Sort',
      align: 'right',
      sortable: true,
      accessor: clause => clause.sort_order,
      render: clause => <span className="tabular-nums">{clause.sort_order}</span>,
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'right',
      render: clause => (
        <div className="flex items-center justify-end gap-2" role="presentation" onClick={e => e.stopPropagation()}>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setEditing(clause)}
            disabled={togglePendingId !== null}
            aria-label={`Edit clause ${clause.code}`}
          >
            Edit
          </Button>
          <LoadingButton
            type="button"
            variant={clause.is_active ? 'ghost' : 'secondary'}
            size="sm"
            loading={togglePendingId === clause.id}
            loadingText={clause.is_active ? 'Retiring…' : 'Reactivating…'}
            disabled={togglePendingId !== null}
            onClick={() => void toggleActive(clause)}
            aria-label={`${clause.is_active ? 'Retire' : 'Reactivate'} clause ${clause.code}`}
          >
            {clause.is_active ? 'Retire' : 'Reactivate'}
          </LoadingButton>
        </div>
      ),
    },
  ];

  return (
    <section aria-labelledby="flowdown-clauses-heading" className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 id="flowdown-clauses-heading" className="text-lg font-semibold">
            Quality clauses
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            The clauses a buyer can select on a purchase order. Default clauses are pre-checked on new purchase orders.
            A clause is never deleted: retire it to stop offering and printing it.
          </p>
        </div>
        <Button onClick={() => setEditing(null)} className="flex items-center self-start">
          <PlusIcon className="mr-2 h-5 w-5" aria-hidden="true" />
          Add clause
        </Button>
      </div>
      <DataTable
        columns={columns}
        data={clauses}
        rowKey={clause => clause.id}
        rowClassName={clause => (clause.is_active ? '' : 'opacity-70')}
        loading={loading}
        error={loadError}
        onRetry={() => void load()}
        pageSize={50}
        dense
        csvExport={{ filename: 'po-quality-clauses' }}
        empty={{
          icon: ShieldCheckIcon,
          title: 'No quality clauses',
          description: 'Add the clauses your supplier quality requirements document defines.',
          action: { label: 'Add clause', onClick: () => setEditing(null) },
        }}
      />
      <QualityClauseDialog
        open={editing !== undefined}
        clause={editing ?? null}
        nextSortOrder={nextSortOrder}
        onCancel={() => setEditing(undefined)}
        onSaved={saved => {
          upsert(saved);
          setEditing(undefined);
          showToast('success', `Clause ${saved.code} saved.`);
        }}
      />
    </section>
  );
}

export function QualityFlowdownSettings() {
  return (
    <div className="space-y-10">
      <FlowdownProfileForm />
      <div className="border-t border-fd-line pt-6">
        <QualityClauseCatalog />
      </div>
    </div>
  );
}

export default QualityFlowdownSettings;
