/**
 * Custom Reports (`/analytics/reports`): build an ad-hoc report over one data source,
 * run it, and keep it as a saved template.
 *
 * Every report endpoint is ADMIN / MANAGER (listing templates is any role, but the
 * screen is gated on `analytics:export` — that same role set — by both the route and
 * the Analytics tab), so this view mounts only for a permitted user and owns all of its
 * own loading: nothing here is fetched for any other Analytics view.
 *
 * Server-gated throughout, so nothing is optimistic: Run, Save, Edit and Delete keep a
 * loading state and show only what the server returned. A 422 / 409 `detail` is shown
 * verbatim. The template open in the builder lives in `?template=<id>` (a query param,
 * not a path segment), so a reload or a shared link reopens it; switching templates
 * while the builder has unsaved changes goes through the app's unsaved-changes prompt.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ExclamationTriangleIcon, PlayIcon, PlusIcon } from '@heroicons/react/24/outline';
import api from '../../../services/api';
import { useAuth } from '../../../context/AuthContext';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import { Button, ConfirmDialog, ErrorState, FormField, LoadingButton, Skeleton, useToast } from '../../ui';
import { apiErrorDetail, apiErrorStatus } from '../../../utils/apiError';
import type { ReportDataSourceCatalog, ReportTemplate, ReportTemplateUpdate } from '../../../types/reportBuilder';
import type { ReportTemplateFormData } from '../../../validation/reportTemplate';
import {
  MAX_LIMIT,
  buildReportRequest,
  draftFromDefinition,
  draftSignature,
  duplicateColumnKeys,
  emptyDraft,
  fieldsOf,
  newColumn,
  newFilter,
  newSort,
  sortFieldOptions,
  ungroupedColumns,
  type BuilderDraft,
} from './reportDefinition';
import { ColumnsEditor } from './ColumnsEditor';
import { FiltersEditor } from './FiltersEditor';
import { GroupByEditor, SortEditor } from './GroupSortEditor';
import { ReportResults, type ReportRunError, type ReportRunOutcome } from './ReportResults';
import { SavedTemplatesPanel, type TemplateAction } from './SavedTemplatesPanel';
import { TemplateFormModal } from './TemplateFormModal';
import { COMPACT_INPUT, SECTION } from './builderStyles';

export const TEMPLATE_PARAM = 'template';

type BuildIssues = { filterErrors: Record<string, string>; errors: string[] };

type ModalState = { mode: 'create' } | { mode: 'edit'; template: ReportTemplate } | null;

type LastRun = { kind: 'builder' } | { kind: 'template'; template: ReportTemplate } | null;

function parseTemplateId(raw: string | null): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const id = Number(raw);
  return id > 0 ? id : null;
}

/** A network failure or a 5xx is worth retrying; a 4xx refusal would repeat itself. */
function isRetryable(err: unknown): boolean {
  const status = apiErrorStatus(err);
  return status === undefined || status >= 500;
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function upsert(templates: ReportTemplate[], template: ReportTemplate): ReportTemplate[] {
  return templates.some(candidate => candidate.id === template.id)
    ? templates.map(candidate => (candidate.id === template.id ? template : candidate))
    : [...templates, template];
}

export default function ReportBuilder() {
  const { user } = useAuth();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const templateParam = parseTemplateId(searchParams.get(TEMPLATE_PARAM));
  const isAdminUser = Boolean(user?.is_superuser || user?.role === 'admin' || user?.role === 'platform_admin');

  // ---- Catalog ---------------------------------------------------------------
  const [catalog, setCatalog] = useState<ReportDataSourceCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const loadCatalog = useCallback(async () => {
    setCatalogError(null);
    try {
      setCatalog(await api.getDataSources());
    } catch (err) {
      setCatalogError(apiErrorDetail(err, 'Could not load the report data sources.'));
    }
  }, []);

  // ---- Saved templates -------------------------------------------------------
  const [templates, setTemplates] = useState<ReportTemplate[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const loadTemplates = useCallback(async () => {
    setTemplatesLoading(true);
    setTemplatesError(null);
    try {
      setTemplates(await api.listReportTemplates());
    } catch (err) {
      setTemplatesError(apiErrorDetail(err, 'Could not load the saved templates.'));
    } finally {
      setTemplatesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    void loadTemplates();
  }, [loadCatalog, loadTemplates]);

  // ---- Builder ---------------------------------------------------------------
  const [draft, setDraft] = useState<BuilderDraft>(emptyDraft);
  const [baseline, setBaseline] = useState(() => draftSignature(emptyDraft()));
  const [issues, setIssues] = useState<BuildIssues | null>(null);
  const [openTemplate, setOpenTemplateState] = useState<ReportTemplate | null>(null);
  // Read synchronously by the ?template= effect, so a save that sets the param does
  // not re-fetch the template it just wrote.
  const openTemplateRef = useRef<ReportTemplate | null>(null);
  const setOpenTemplate = useCallback((template: ReportTemplate | null) => {
    openTemplateRef.current = template;
    setOpenTemplateState(template);
  }, []);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [templateLoadError, setTemplateLoadError] = useState<string | null>(null);
  const [templateReloadKey, setTemplateReloadKey] = useState(0);

  const signature = draftSignature(draft);
  const isDirty = signature !== baseline;
  const { markSaved, confirmDiscard, allowInternalNavigation } = useUnsavedChanges(isDirty);

  const resetBuilder = useCallback(() => {
    const fresh = emptyDraft();
    setDraft(fresh);
    setBaseline(draftSignature(fresh));
    setIssues(null);
  }, []);

  const loadIntoBuilder = useCallback(
    (template: ReportTemplate, source: ReportDataSourceCatalog) => {
      const next = draftFromDefinition(template, source);
      setDraft(next);
      setBaseline(draftSignature(next));
      setIssues(null);
      setOpenTemplate(template);
    },
    [setOpenTemplate]
  );

  // `?template=` is the source of truth for which template is open.
  useEffect(() => {
    if (!catalog) return undefined;
    if (templateParam === null) {
      setTemplateLoadError(null);
      setTemplateLoading(false);
      if (openTemplateRef.current) {
        setOpenTemplate(null);
        resetBuilder();
      }
      return undefined;
    }
    if (openTemplateRef.current?.id === templateParam) return undefined;
    let cancelled = false;
    setTemplateLoading(true);
    setTemplateLoadError(null);
    api
      .getReportTemplate(templateParam)
      .then(template => {
        if (!cancelled) loadIntoBuilder(template, catalog);
      })
      .catch(err => {
        if (!cancelled) setTemplateLoadError(apiErrorDetail(err, 'Could not open this template.'));
      })
      .finally(() => {
        if (!cancelled) setTemplateLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [catalog, templateParam, templateReloadKey, loadIntoBuilder, resetBuilder, setOpenTemplate]);

  const setTemplateParam = useCallback(
    (id: number | null, bypassPrompt: boolean) => {
      const apply = () =>
        setSearchParams(current => {
          const next = new URLSearchParams(current);
          if (id === null) next.delete(TEMPLATE_PARAM);
          else next.set(TEMPLATE_PARAM, String(id));
          return next;
        });
      if (bypassPrompt) allowInternalNavigation(apply);
      else apply();
    },
    [allowInternalNavigation, setSearchParams]
  );

  const fields = useMemo(() => (catalog ? fieldsOf(catalog, draft.data_source) : []), [catalog, draft.data_source]);
  const sortOptions = useMemo(() => (catalog ? sortFieldOptions(draft, catalog) : []), [catalog, draft]);
  const missingGroups = ungroupedColumns(draft);
  const duplicateKeys = duplicateColumnKeys(draft);
  const labelOf = (name: string) => fields.find(field => field.name === name)?.label ?? name;

  const patchDraft = (changes: Partial<BuilderDraft>) => setDraft(current => ({ ...current, ...changes }));

  const changeDataSource = (dataSource: string) =>
    setDraft(current =>
      current.data_source === dataSource ? current : { ...emptyDraft(), limit: current.limit, data_source: dataSource }
    );

  const addColumn = () => {
    const unused = fields.find(field => !draft.columns.some(column => column.field === field.name)) ?? fields[0];
    if (unused) patchDraft({ columns: [...draft.columns, newColumn(unused.name)] });
  };

  // ---- Running ---------------------------------------------------------------
  const [outcome, setOutcome] = useState<ReportRunOutcome | null>(null);
  const [runError, setRunError] = useState<ReportRunError | null>(null);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState<{ id: number; action: TemplateAction } | null>(null);
  const lastRun = useRef<LastRun>(null);

  const runBuilder = async () => {
    if (!catalog || running) return;
    const built = buildReportRequest(draft, catalog);
    if (!built.ok) {
      setIssues({ filterErrors: built.filterErrors, errors: built.errors });
      return;
    }
    setIssues(null);
    lastRun.current = { kind: 'builder' };
    setRunning(true);
    setRunError(null);
    const title = openTemplate ? `${openTemplate.name}${isDirty ? ' (unsaved changes)' : ''}` : 'Unsaved report';
    try {
      const result = await api.runCustomReport(built.request);
      setOutcome({ title, definition: built.definition, result });
    } catch (err) {
      setOutcome(null);
      setRunError({
        title: 'The report did not run',
        message: apiErrorDetail(err, 'The report could not be run.'),
        retryable: isRetryable(err),
      });
    } finally {
      setRunning(false);
    }
  };

  const runTemplate = async (template: ReportTemplate) => {
    if (running) return;
    lastRun.current = { kind: 'template', template };
    setBusy({ id: template.id, action: 'run' });
    setRunning(true);
    setRunError(null);
    try {
      const result = await api.runReportTemplate(template.id);
      setOutcome({ title: template.name, definition: template, result });
    } catch (err) {
      setOutcome(null);
      setRunError({
        title: `${template.name} did not run`,
        message: apiErrorDetail(err, 'The template could not be run.'),
        retryable: isRetryable(err),
      });
      // A 409 means the stored definition stopped running: refresh its "needs editing" flag.
      if (apiErrorStatus(err) === 409) void loadTemplates();
    } finally {
      setBusy(null);
      setRunning(false);
    }
  };

  const retryRun = () => {
    const last = lastRun.current;
    if (last?.kind === 'template') void runTemplate(last.template);
    else void runBuilder();
  };

  const exportTemplate = async (template: ReportTemplate) => {
    setBusy({ id: template.id, action: 'export' });
    try {
      const { blob, filename } = await api.exportReportTemplateCsv(template.id);
      saveDownload(blob, filename);
      showToast('success', `Exported ${filename}.`);
    } catch (err) {
      showToast('error', apiErrorDetail(err, `Could not export ${template.name}.`));
      if (apiErrorStatus(err) === 409) void loadTemplates();
    } finally {
      setBusy(null);
    }
  };

  // ---- Opening / starting over ------------------------------------------------
  const openFromList = (template: ReportTemplate) => {
    if (template.id === templateParam && openTemplateRef.current?.id === template.id) {
      // Re-opening the open template reverts the builder to what is saved.
      if (!isDirty || !catalog || !confirmDiscard()) return;
      loadIntoBuilder(template, catalog);
      return;
    }
    // The router's unsaved-changes prompt guards this when the builder is dirty.
    setTemplateParam(template.id, false);
  };

  const startNewReport = () => {
    if (templateParam !== null) {
      setTemplateParam(null, false);
      return;
    }
    if (!confirmDiscard()) return;
    resetBuilder();
  };

  // ---- Saving ----------------------------------------------------------------
  const [modal, setModal] = useState<ModalState>(null);
  const [saving, setSaving] = useState(false);
  const [savingDefinition, setSavingDefinition] = useState(false);

  /** The builder's definition, or null after surfacing why it is not one yet. */
  const currentDefinition = () => {
    if (!catalog) return null;
    const built = buildReportRequest(draft, catalog);
    if (!built.ok) {
      setIssues({ filterErrors: built.filterErrors, errors: built.errors });
      return null;
    }
    setIssues(null);
    return built.definition;
  };

  const openSaveAsNew = () => {
    if (currentDefinition()) setModal({ mode: 'create' });
  };

  const saveDefinitionChanges = async () => {
    const template = openTemplate;
    if (!template || savingDefinition) return;
    const definition = currentDefinition();
    if (!definition) return;
    setSavingDefinition(true);
    try {
      const updated = await api.updateReportTemplate(template.id, { definition });
      markSaved();
      setBaseline(signature);
      setOpenTemplate(updated);
      setTemplates(current => upsert(current, updated));
      showToast('success', `Saved changes to "${updated.name}".`);
    } catch (err) {
      showToast('error', apiErrorDetail(err, `Could not save changes to ${template.name}.`));
    } finally {
      setSavingDefinition(false);
    }
  };

  const submitModal = async (data: ReportTemplateFormData) => {
    if (!modal || saving) return;
    if (modal.mode === 'create') {
      const definition = currentDefinition();
      if (!definition) {
        setModal(null);
        return;
      }
      setSaving(true);
      try {
        const created = await api.createReportTemplate({
          name: data.name,
          description: data.description,
          is_shared: data.is_shared,
          ...definition,
        });
        markSaved();
        setBaseline(signature);
        setOpenTemplate(created);
        setTemplates(current => upsert(current, created));
        setModal(null);
        showToast('success', `Saved "${created.name}".`);
        setTemplateParam(created.id, true);
      } catch (err) {
        showToast('error', apiErrorDetail(err, 'Could not save the template.'));
      } finally {
        setSaving(false);
      }
      return;
    }

    const { template } = modal;
    const changes: ReportTemplateUpdate = {
      name: data.name,
      description: data.description,
      is_shared: data.is_shared,
    };
    const withDefinition = data.include_definition && openTemplate?.id === template.id && isDirty;
    if (withDefinition) {
      const definition = currentDefinition();
      if (!definition) {
        showToast('error', 'Finish the report in the builder before saving its definition, or untick that option.');
        return;
      }
      changes.definition = definition;
    }
    setSaving(true);
    try {
      const updated = await api.updateReportTemplate(template.id, changes);
      if (withDefinition) {
        markSaved();
        setBaseline(signature);
      }
      if (openTemplateRef.current?.id === updated.id) setOpenTemplate(updated);
      setTemplates(current => upsert(current, updated));
      setModal(null);
      showToast('success', `Saved "${updated.name}".`);
    } catch (err) {
      showToast('error', apiErrorDetail(err, `Could not save ${template.name}.`));
    } finally {
      setSaving(false);
    }
  };

  // ---- Deleting --------------------------------------------------------------
  const [deleting, setDeleting] = useState<ReportTemplate | null>(null);
  const [deletePending, setDeletePending] = useState(false);

  const confirmDelete = async () => {
    const template = deleting;
    if (!template || deletePending) return;
    setDeletePending(true);
    try {
      await api.deleteReportTemplate(template.id);
      setTemplates(current => current.filter(candidate => candidate.id !== template.id));
      setDeleting(null);
      showToast('success', `Deleted "${template.name}".`);
      if (openTemplateRef.current?.id === template.id) {
        // Edits to a template that no longer exists have nothing left to be saved onto.
        setOpenTemplate(null);
        resetBuilder();
        setTemplateParam(null, true);
      }
    } catch (err) {
      showToast('error', apiErrorDetail(err, `Could not delete ${template.name}.`));
    } finally {
      setDeletePending(false);
    }
  };

  const canManage = useCallback(
    (template: ReportTemplate) => isAdminUser || (user?.id !== undefined && template.created_by === user.id),
    [isAdminUser, user?.id]
  );

  const modalTemplate = modal?.mode === 'edit' ? modal.template : null;
  const modalInitial = modalTemplate
    ? { name: modalTemplate.name, description: modalTemplate.description, is_shared: modalTemplate.is_shared }
    : {
        name: openTemplate ? `${openTemplate.name} (copy)`.slice(0, 255) : '',
        description: openTemplate?.description ?? null,
        is_shared: false,
      };

  // ---- Render ----------------------------------------------------------------
  const sources = catalog ? Object.entries(catalog) : [];
  const busyEditing = running || saving || savingDefinition;

  return (
    <div className="space-y-4">
      <SavedTemplatesPanel
        templates={templates}
        catalog={catalog ?? {}}
        loading={templatesLoading}
        error={templatesError}
        onRetry={() => void loadTemplates()}
        openTemplateId={openTemplate?.id ?? null}
        busy={busy}
        canManage={canManage}
        onOpen={openFromList}
        onRun={template => void runTemplate(template)}
        onExport={template => void exportTemplate(template)}
        onEdit={template => setModal({ mode: 'edit', template })}
        onDelete={template => setDeleting(template)}
      />

      <section aria-labelledby="report-builder-heading" className="card card-compact space-y-3">
        <header className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 id="report-builder-heading" className="text-base font-semibold text-fd-ink">
              Report builder
            </h2>
            <p className="text-xs text-fd-mute">
              {openTemplate ? (
                <>
                  Editing template <span className="text-fd-body">{openTemplate.name}</span>
                </>
              ) : (
                'New report'
              )}
              {isDirty && <span className="ml-2 text-amber-300">Unsaved changes</span>}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="inline-flex items-center gap-1"
            onClick={startNewReport}
            disabled={busyEditing || (!openTemplate && !isDirty && templateParam === null)}
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            New report
          </Button>
        </header>

        {catalogError ? (
          <ErrorState
            title="Couldn't load the report data sources"
            message={catalogError}
            onRetry={() => void loadCatalog()}
          />
        ) : !catalog ? (
          <div className="space-y-2" aria-busy="true">
            <Skeleton className="h-9 w-64" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : templateLoadError ? (
          <div className="space-y-2">
            <ErrorState
              title="Couldn't open this template"
              message={templateLoadError}
              onRetry={() => setTemplateReloadKey(key => key + 1)}
            />
            <Button variant="secondary" size="sm" onClick={() => setTemplateParam(null, true)}>
              Start a new report instead
            </Button>
          </div>
        ) : templateLoading ? (
          <p className="text-sm text-fd-mute" role="status">
            Opening template…
          </p>
        ) : (
          <>
            {openTemplate && openTemplate.definition_errors.length > 0 && (
              <div
                role="note"
                className="rounded-sm border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
              >
                <p className="font-semibold">This template needs editing before it can run as saved:</p>
                <ul className="mt-1 list-disc pl-5 text-xs">
                  {openTemplate.definition_errors.map(message => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-12 sm:items-end">
              <FormField label="Data source" required className="sm:col-span-6">
                {props => (
                  <select
                    {...props}
                    className={COMPACT_INPUT}
                    value={draft.data_source}
                    disabled={busyEditing}
                    onChange={event => changeDataSource(event.target.value)}
                  >
                    <option value="">Choose a data source…</option>
                    {draft.data_source && !catalog[draft.data_source] && (
                      <option value={draft.data_source}>{`${draft.data_source} (no longer available)`}</option>
                    )}
                    {sources.map(([key, info]) => (
                      <option key={key} value={key}>
                        {info.label}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Row limit" className="sm:col-span-3" help={`1 to ${MAX_LIMIT.toLocaleString()}`}>
                {props => (
                  <input
                    {...props}
                    type="number"
                    min={1}
                    max={MAX_LIMIT}
                    step={1}
                    className={COMPACT_INPUT}
                    value={draft.limit}
                    disabled={busyEditing}
                    onChange={event => patchDraft({ limit: event.target.value })}
                  />
                )}
              </FormField>
            </div>

            {draft.data_source && (
              <>
                <ColumnsEditor
                  fields={fields}
                  columns={draft.columns}
                  onChange={columns => patchDraft({ columns })}
                  onAdd={addColumn}
                  disabled={busyEditing}
                />
                <FiltersEditor
                  fields={fields}
                  filters={draft.filters}
                  errors={issues?.filterErrors ?? {}}
                  onChange={filters => patchDraft({ filters })}
                  onAdd={() => patchDraft({ filters: [...draft.filters, newFilter(fields[0])] })}
                  disabled={busyEditing}
                />
                <GroupByEditor
                  fields={fields}
                  groupBy={draft.group_by}
                  onChange={group_by => patchDraft({ group_by })}
                  disabled={busyEditing}
                />
                <SortEditor
                  options={sortOptions}
                  sort={draft.sort}
                  onChange={sort => patchDraft({ sort })}
                  onAdd={() => patchDraft({ sort: [...draft.sort, newSort(sortOptions[0]?.value ?? '')] })}
                  disabled={busyEditing}
                />
              </>
            )}

            {(missingGroups.length > 0 || duplicateKeys.length > 0) && (
              <div
                role="note"
                className="space-y-2 rounded-sm border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
              >
                {missingGroups.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <ExclamationTriangleIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
                    <span>
                      {`A grouped report needs every column without a summary in Group by: ${missingGroups
                        .map(labelOf)
                        .join(', ')}.`}
                    </span>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busyEditing}
                      onClick={() => patchDraft({ group_by: [...draft.group_by, ...missingGroups] })}
                    >
                      Group by {missingGroups.length === 1 ? 'it' : 'them'}
                    </Button>
                  </div>
                )}
                {duplicateKeys.length > 0 && (
                  <p className="flex items-center gap-2">
                    <ExclamationTriangleIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {`Column names must be unique; give one of these a different label: ${duplicateKeys.join(', ')}.`}
                  </p>
                )}
              </div>
            )}

            {issues && issues.errors.length > 0 && (
              <div role="alert">
                <ul aria-label="Report problems" className="list-disc space-y-0.5 pl-5 text-sm text-fd-red">
                  {issues.errors.map(message => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className={`${SECTION} flex flex-wrap items-center gap-2`}>
              <LoadingButton
                loading={running && lastRun.current?.kind === 'builder'}
                disabled={running}
                onClick={() => void runBuilder()}
                className="inline-flex items-center gap-1.5"
              >
                <PlayIcon className="h-4 w-4" aria-hidden="true" />
                Run report
              </LoadingButton>
              {openTemplate && canManage(openTemplate) && (
                <LoadingButton
                  variant="secondary"
                  loading={savingDefinition}
                  disabled={!isDirty || busyEditing}
                  onClick={() => void saveDefinitionChanges()}
                >
                  Save changes
                </LoadingButton>
              )}
              <Button variant="secondary" onClick={openSaveAsNew} disabled={busyEditing || !draft.data_source}>
                Save as new
              </Button>
            </div>
          </>
        )}
      </section>

      <ReportResults outcome={outcome} error={runError} running={running} catalog={catalog ?? {}} onRetry={retryRun} />

      <TemplateFormModal
        open={modal !== null}
        mode={modal?.mode ?? 'create'}
        initial={modalInitial}
        offerDefinition={Boolean(modalTemplate && openTemplate?.id === modalTemplate.id && isDirty)}
        pending={saving}
        onSubmit={data => void submitModal(data)}
        onCancel={() => setModal(null)}
      />

      <ConfirmDialog
        open={deleting !== null}
        title="Delete report template?"
        message={deleting ? `"${deleting.name}" will be deleted for everyone it is shared with.` : ''}
        confirmLabel="Delete template"
        variant="danger"
        pending={deletePending}
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          if (!deletePending) setDeleting(null);
        }}
      />
    </div>
  );
}
