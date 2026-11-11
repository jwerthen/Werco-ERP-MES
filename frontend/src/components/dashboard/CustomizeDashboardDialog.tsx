/**
 * Customize dashboard — choose which widgets appear, their order and (panels) width.
 *
 * Mount it ONLY while open: the draft is lazy state seeded once from the saved layout, so
 * the page's polls and re-resolves never reset it mid-edit. Escape, the backdrop, the X
 * and Cancel all go through the same unsaved-changes guard; while a save or reset is in
 * flight nothing can dismiss it (the server may still apply the write).
 *
 * Only widgets the viewer is eligible for are listed. Saved widgets they cannot see right
 * now are carried through a save unchanged (`buildSavePayload`), so a temporary
 * permission change never destroys a layout.
 *
 * Reordering is ↑/↓ buttons (no drag-and-drop): focus stays on the moved row's control
 * and a polite live region announces the new position. Focus never drops to <body>: a
 * 409 on save or reset moves it to "Reload saved layout", which stays mounted (with its
 * spinner) while the reload runs; a successful reload focuses the dialog heading and
 * announces that the unsaved edits were discarded.
 */
import React, { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ChevronDownIcon, ChevronUpIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { Button, ConfirmDialog, LoadingButton, Modal, useToast } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { DashboardLayoutValues } from '../../types/dashboardLayout';
import { apiErrorDetail, apiErrorStatus } from '../../utils/apiError';
import {
  buildSavePayload,
  countCarriedWidgets,
  draftFromLayout,
  isSameLayout,
  isWidgetEligible,
  type LayoutDraft,
} from './dashboardLayout';
import { DashboardLayoutCompanyChangedError } from './useDashboardLayout';
import { DASHBOARD_REGISTRY, WIDGET_CATEGORY_ORDER, type WidgetRegistry } from './widgetRegistry';
import {
  PANEL_SIZE_LABELS,
  PANEL_SIZES,
  isPanelSize,
  type DashboardWidgetDefinition,
  type PanelSize,
  type WidgetEligibilityContext,
} from './widgetTypes';

export const READ_ONLY_LAYOUT_NOTICE =
  "This session can't save a dashboard layout — read-only company view or an API/kiosk credential.";

export interface CustomizeDashboardDialogProps {
  /** The saved layout (null = the stock default). */
  saved: DashboardLayoutValues | null;
  canEdit: boolean;
  eligibility: WidgetEligibilityContext;
  /** Save; rejects with the request error (409 = changed in another tab). */
  onSave: (layout: DashboardLayoutValues) => Promise<unknown>;
  /** POST /dashboard/layout/reset; rejects with the request error. */
  onReset: () => Promise<unknown>;
  /** Re-read the saved layout; resolves with it (null = default) or rejects. */
  onReload: () => Promise<DashboardLayoutValues | null>;
  onClose: () => void;
  registry?: WidgetRegistry;
}

type Pending = 'save' | 'reset' | 'reload' | null;
type ListZone = 'kpi' | 'panel';

const safeId = (widgetId: string) => widgetId.replace(/[^a-zA-Z0-9_-]/g, '-');

export function CustomizeDashboardDialog({
  saved,
  canEdit,
  eligibility,
  onSave,
  onReset,
  onReload,
  onClose,
  registry = DASHBOARD_REGISTRY,
}: CustomizeDashboardDialogProps) {
  const baseId = useId().replace(/:/g, '');
  const { showToast } = useToast();
  const [draft, setDraft] = useState<LayoutDraft>(() => draftFromLayout(saved, eligibility, registry));
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [notice, setNotice] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [confirmReset, setConfirmReset] = useState(false);
  const pendingFocusRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const payload = useMemo(
    () => buildSavePayload(draft, saved, eligibility, registry),
    [draft, saved, eligibility, registry]
  );
  const isDirty = !isSameLayout(payload, saved, registry);
  const { confirmDiscard, markSaved } = useUnsavedChanges(isDirty);
  const carriedCount = useMemo(() => countCarriedWidgets(saved, eligibility, registry), [saved, eligibility, registry]);

  const eligible = useMemo(() => {
    const byZone = {
      banner: [] as DashboardWidgetDefinition[],
      kpi: [] as DashboardWidgetDefinition[],
      panel: [] as DashboardWidgetDefinition[],
    };
    registry.widgets.forEach(definition => {
      if (isWidgetEligible(definition, eligibility)) byZone[definition.zone].push(definition);
    });
    return byZone;
  }, [registry, eligibility]);

  const controlsDisabled = !canEdit || pending !== null || needsRefresh;
  const dialogHeadingId = `${baseId}-heading`;
  const reloadButtonId = `${baseId}-reload`;

  // Restore focus after a move/add/remove re-rendered the lists.
  useLayoutEffect(() => {
    const target = pendingFocusRef.current;
    if (!target) return;
    pendingFocusRef.current = null;
    document.getElementById(target)?.focus();
  });

  const elementId = (action: 'up' | 'down' | 'remove' | 'add' | 'size', widgetId: string) =>
    `${baseId}-${action}-${safeId(widgetId)}`;

  const titleOf = useCallback((widgetId: string) => registry.byId.get(widgetId)?.title ?? widgetId, [registry]);

  const requestClose = useCallback(() => {
    if (pending !== null) return;
    if (confirmDiscard()) onClose();
  }, [pending, confirmDiscard, onClose]);

  // ── List editing ───────────────────────────────────────────────────────────
  const listIds = (zone: ListZone, current: LayoutDraft) =>
    zone === 'kpi' ? current.kpis : current.panels.map(panel => panel.id);

  const move = (zone: ListZone, index: number, direction: -1 | 1) => {
    const ids = listIds(zone, draft);
    const target = index + direction;
    if (target < 0 || target >= ids.length) return;
    const widgetId = ids[index];
    setDraft(current => {
      if (zone === 'kpi') {
        const kpis = [...current.kpis];
        [kpis[index], kpis[target]] = [kpis[target], kpis[index]];
        return { ...current, kpis };
      }
      const panels = [...current.panels];
      [panels[index], panels[target]] = [panels[target], panels[index]];
      return { ...current, panels };
    });
    const atEdge = direction === -1 ? target === 0 : target === ids.length - 1;
    const keep: 'up' | 'down' = direction === -1 ? 'up' : 'down';
    const opposite: 'up' | 'down' = direction === -1 ? 'down' : 'up';
    pendingFocusRef.current = elementId(atEdge ? opposite : keep, widgetId);
    setAnnouncement(`${titleOf(widgetId)} moved to position ${target + 1} of ${ids.length}`);
  };

  const remove = (zone: ListZone, index: number) => {
    const ids = listIds(zone, draft);
    const widgetId = ids[index];
    const remaining = ids.filter((_, position) => position !== index);
    setDraft(current =>
      zone === 'kpi'
        ? { ...current, kpis: current.kpis.filter(id => id !== widgetId) }
        : { ...current, panels: current.panels.filter(panel => panel.id !== widgetId) }
    );
    pendingFocusRef.current =
      remaining.length > 0
        ? elementId('remove', remaining[Math.min(index, remaining.length - 1)])
        : elementId('add', widgetId);
    setAnnouncement(`${titleOf(widgetId)} removed from your dashboard`);
  };

  const add = (zone: ListZone, definition: DashboardWidgetDefinition) => {
    const length = listIds(zone, draft).length + 1;
    setDraft(current =>
      zone === 'kpi'
        ? { ...current, kpis: [...current.kpis, definition.id] }
        : {
            ...current,
            panels: [
              ...current.panels,
              { id: definition.id, size: isPanelSize(definition.defaultSize) ? definition.defaultSize : 'half' },
            ],
          }
    );
    pendingFocusRef.current = elementId(length > 1 ? 'up' : 'remove', definition.id);
    setAnnouncement(`${definition.title} added at position ${length} of ${length}`);
  };

  const setSize = (index: number, size: PanelSize) => {
    setDraft(current => ({
      ...current,
      panels: current.panels.map((panel, position) => (position === index ? { ...panel, size } : panel)),
    }));
  };

  const toggleBanner = (widgetId: string, checked: boolean) => {
    setDraft(current => ({
      ...current,
      banners: checked
        ? [...current.banners.filter(id => id !== widgetId), widgetId]
        : current.banners.filter(id => id !== widgetId),
    }));
  };

  // ── Server actions (non-optimistic) ────────────────────────────────────────
  /** Show the failure; a stale write (409 / company changed) also sends focus to "Reload saved layout". */
  const describeFailure = (failure: unknown, fallback: string) => {
    if (apiErrorStatus(failure) === 409 || failure instanceof DashboardLayoutCompanyChangedError) {
      setNeedsRefresh(true);
      // Save and Reset are disabled from here on; the recovery action is the next step.
      pendingFocusRef.current = reloadButtonId;
    }
    setError(apiErrorDetail(failure, fallback));
  };

  const handleSave = async () => {
    if (!isDirty || controlsDisabled) return;
    setPending('save');
    setError('');
    setNotice('');
    try {
      await onSave(payload);
      if (!mountedRef.current) return;
      markSaved();
      showToast('success', 'Dashboard layout saved');
      onClose();
    } catch (failure) {
      if (!mountedRef.current) return;
      describeFailure(failure, 'Your dashboard layout was not saved. Try again.');
      setPending(null);
    }
  };

  const handleReset = async () => {
    setPending('reset');
    setError('');
    setNotice('');
    try {
      await onReset();
      if (!mountedRef.current) return;
      markSaved();
      setConfirmReset(false);
      showToast('success', 'Dashboard reset to default');
      onClose();
    } catch (failure) {
      if (!mountedRef.current) return;
      setConfirmReset(false);
      describeFailure(failure, 'Your dashboard was not reset. Try again.');
      setPending(null);
    }
  };

  // The error (and the button inside it) stays up until the reload settles, so the focused
  // control never unmounts under the user and its spinner is visible.
  const handleReload = async () => {
    setPending('reload');
    setNotice('');
    try {
      const reloaded = await onReload();
      if (!mountedRef.current) return;
      setDraft(draftFromLayout(reloaded, eligibility, registry));
      setError('');
      setNeedsRefresh(false);
      setNotice('Reloaded your saved layout. Your unsaved changes were discarded.');
      pendingFocusRef.current = dialogHeadingId;
    } catch (failure) {
      if (!mountedRef.current) return;
      setError(apiErrorDetail(failure, 'Your saved layout could not be reloaded. Try again.'));
      pendingFocusRef.current = reloadButtonId;
    } finally {
      if (mountedRef.current) setPending(null);
    }
  };

  // ── Rendering ──────────────────────────────────────────────────────────────
  const renderList = (zone: ListZone, heading: string) => {
    const ids = listIds(zone, draft);
    const onDashboard = ids
      .map((id, index) => ({ id, index, definition: registry.byId.get(id) }))
      .filter((row): row is { id: string; index: number; definition: DashboardWidgetDefinition } =>
        Boolean(row.definition)
      );
    const shown = new Set(ids);
    const available = eligible[zone].filter(definition => !shown.has(definition.id));
    const headingId = `${baseId}-${zone}-heading`;
    const onListId = `${baseId}-${zone}-on`;
    const availableId = `${baseId}-${zone}-available`;
    return (
      <section aria-labelledby={headingId} className="space-y-2">
        <h3 id={headingId} className="text-sm font-semibold text-fd-ink">
          {heading}
        </h3>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="min-w-0 rounded-sm border border-fd-line p-2">
            <p id={onListId} className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
              On your dashboard
            </p>
            {onDashboard.length === 0 ? (
              <p className="py-1.5 text-xs text-slate-400">Nothing here yet — add from the list.</p>
            ) : (
              <ol aria-labelledby={onListId} className="divide-y divide-fd-line">
                {onDashboard.map(({ id, index, definition }) => {
                  const panel = zone === 'panel' ? draft.panels[index] : null;
                  // The name grows from a real basis (not flex-1's 0), so on a narrow row the
                  // control group wraps below it instead of squeezing the name to "…" (and, at
                  // phone width, wraps within itself); a long name wraps rather than truncating.
                  return (
                    <li key={id} className="flex flex-wrap items-center gap-1.5 py-1.5">
                      <span className="w-5 flex-shrink-0 text-right text-[10px] tabular-nums text-slate-400">
                        {index + 1}
                      </span>
                      <span
                        data-testid="widget-row-title"
                        className="min-w-0 grow basis-40 break-words text-sm text-slate-200"
                        title={definition.description}
                      >
                        {definition.title}
                      </span>
                      <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-1.5">
                        {panel && (
                          <>
                            <label htmlFor={elementId('size', id)} className="sr-only">
                              Width of {definition.title}
                            </label>
                            <select
                              id={elementId('size', id)}
                              className="input !w-auto !py-0.5 !text-xs"
                              value={panel.size}
                              disabled={controlsDisabled}
                              onChange={event => setSize(index, event.target.value as PanelSize)}
                            >
                              {PANEL_SIZES.map(size => (
                                <option key={size} value={size}>
                                  {PANEL_SIZE_LABELS[size]}
                                </option>
                              ))}
                            </select>
                          </>
                        )}
                        <button
                          type="button"
                          id={elementId('up', id)}
                          className="btn-ghost btn-sm !px-1"
                          aria-label={`Move ${definition.title} up`}
                          title="Move up"
                          disabled={controlsDisabled || index === 0}
                          onClick={() => move(zone, index, -1)}
                        >
                          <ChevronUpIcon aria-hidden="true" className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          id={elementId('down', id)}
                          className="btn-ghost btn-sm !px-1"
                          aria-label={`Move ${definition.title} down`}
                          title="Move down"
                          disabled={controlsDisabled || index === ids.length - 1}
                          onClick={() => move(zone, index, 1)}
                        >
                          <ChevronDownIcon aria-hidden="true" className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          id={elementId('remove', id)}
                          className="btn-ghost btn-sm !px-1.5 text-xs"
                          aria-label={`Remove ${definition.title}`}
                          disabled={controlsDisabled}
                          onClick={() => remove(zone, index)}
                        >
                          Remove
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
          <div className="min-w-0 rounded-sm border border-fd-line p-2">
            <p id={availableId} className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
              Available
            </p>
            {available.length === 0 ? (
              <p className="py-1.5 text-xs text-slate-400">Everything available is on your dashboard.</p>
            ) : (
              <div aria-labelledby={availableId} role="group" className="space-y-2">
                {WIDGET_CATEGORY_ORDER.map((category, categoryIndex) => {
                  const inCategory = available.filter(definition => definition.category === category);
                  if (inCategory.length === 0) return null;
                  // Index, not the label: category labels contain spaces.
                  const categoryLabelId = `${availableId}-category-${categoryIndex}`;
                  return (
                    <div key={category}>
                      <p
                        id={categoryLabelId}
                        className="text-[10px] font-medium uppercase tracking-wide text-slate-400"
                      >
                        {category}
                      </p>
                      <ul aria-labelledby={categoryLabelId} className="divide-y divide-fd-line">
                        {inCategory.map(definition => (
                          <li key={definition.id} className="flex items-start gap-2 py-1.5">
                            <div className="min-w-0 flex-1">
                              <p className="text-sm text-slate-200">{definition.title}</p>
                              <p className="text-xs text-slate-400">{definition.description}</p>
                            </div>
                            <button
                              type="button"
                              id={elementId('add', definition.id)}
                              className="btn-ghost btn-sm !px-1.5 text-xs"
                              aria-label={`Add ${definition.title}`}
                              disabled={controlsDisabled}
                              onClick={() => add(zone, definition)}
                            >
                              Add
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </section>
    );
  };

  const bannerHeadingId = `${baseId}-banner-heading`;

  return (
    <Modal
      open
      onClose={requestClose}
      size="3xl"
      ariaLabel="Customize dashboard"
      closeOnEscape={pending === null}
      closeOnBackdrop={pending === null}
    >
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 id={dialogHeadingId} tabIndex={-1} className="text-lg font-semibold text-fd-ink">
              Customize dashboard
            </h2>
            <p className="text-sm text-slate-400">
              Choose what appears on your dashboard, in what order, and how wide each panel is. Your layout is saved to
              your account for this company.
            </p>
          </div>
          <button
            type="button"
            className="btn-ghost btn-sm !px-1"
            aria-label="Close customize dashboard"
            disabled={pending !== null}
            onClick={requestClose}
          >
            <XMarkIcon aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>

        {!canEdit && (
          <p className="rounded-sm border border-fd-line bg-slate-800/40 p-2.5 text-sm text-slate-300">
            {READ_ONLY_LAYOUT_NOTICE}
          </p>
        )}
        {carriedCount > 0 && (
          <p className="text-xs text-slate-400">
            {carriedCount === 1
              ? '1 widget in your saved layout is not available to you right now; it is kept and will reappear if access returns.'
              : `${carriedCount} widgets in your saved layout are not available to you right now; they are kept and will reappear if access returns.`}
          </p>
        )}
        {error && (
          <div role="alert" className="rounded-sm border border-red-500/40 bg-red-500/10 p-2.5 text-sm text-red-200">
            <p>{error}</p>
            {needsRefresh && (
              <LoadingButton
                id={reloadButtonId}
                variant="secondary"
                size="sm"
                className="mt-2"
                loading={pending === 'reload'}
                disabled={pending !== null && pending !== 'reload'}
                onClick={handleReload}
              >
                Reload saved layout
              </LoadingButton>
            )}
          </div>
        )}
        {/* Always mounted, so a notice written into it is announced (the reload's "edits
            discarded" message must not change the draft silently). */}
        <div role="status" aria-live="polite">
          {notice && <p className="text-sm text-slate-300">{notice}</p>}
        </div>

        <section aria-labelledby={bannerHeadingId} className="space-y-2">
          <h3 id={bannerHeadingId} className="text-sm font-semibold text-fd-ink">
            Alerts &amp; banners
          </h3>
          <ul className="divide-y divide-fd-line rounded-sm border border-fd-line px-2">
            {eligible.banner.map(definition => {
              const checkboxId = `${baseId}-banner-${safeId(definition.id)}`;
              const descriptionId = `${checkboxId}-description`;
              // The label is the title alone (the checkbox's name); the description is
              // attached as its accessible description, not run into the name.
              return (
                <li key={definition.id} className="flex items-start gap-2 py-1.5">
                  <input
                    id={checkboxId}
                    type="checkbox"
                    className="checkbox checkbox-sm mt-0.5"
                    aria-describedby={descriptionId}
                    checked={draft.banners.includes(definition.id)}
                    disabled={controlsDisabled}
                    onChange={event => toggleBanner(definition.id, event.target.checked)}
                  />
                  <div className="min-w-0 flex-1">
                    <label htmlFor={checkboxId} className="block text-sm text-slate-200">
                      {definition.title}
                    </label>
                    <p id={descriptionId} className="text-xs text-slate-400">
                      {definition.description}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>

        {renderList('kpi', 'Key metrics')}
        {renderList('panel', 'Panels')}

        <p role="status" aria-live="polite" className="sr-only">
          {announcement}
        </p>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-fd-line pt-3">
          <Button
            variant="ghost"
            size="sm"
            disabled={!canEdit || pending !== null || needsRefresh || saved === null}
            title={saved === null ? 'You are already using the default layout' : undefined}
            onClick={() => setConfirmReset(true)}
          >
            Reset to default
          </Button>
          <div className="flex items-center gap-2">
            <Button variant="secondary" disabled={pending !== null} onClick={requestClose}>
              Cancel
            </Button>
            <LoadingButton loading={pending === 'save'} disabled={!isDirty || controlsDisabled} onClick={handleSave}>
              Save layout
            </LoadingButton>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmReset}
        title="Reset dashboard to default?"
        message="Your saved layout will be replaced by the standard dashboard."
        confirmLabel="Reset to default"
        variant="warning"
        pending={pending === 'reset'}
        onConfirm={handleReset}
        onCancel={() => setConfirmReset(false)}
      />
    </Modal>
  );
}

export default CustomizeDashboardDialog;
