/**
 * "Add widgets": the edit-mode catalog, a shared `<Modal size="3xl">` on every viewport.
 *
 * Contract:
 * - Pass `editor` = `useLayoutEditor()`; it is open while `editor.galleryOpen`. Escape, the
 *   backdrop, ✕ and Done all call `editor.closeGallery()` WITHOUT confirmation — nothing is
 *   lost, every addition is already in the draft (and on the grid behind it).
 * - Sections, in order: **Alerts & banners** (checkboxes toggling `setBannerVisible`; the
 *   label is the title, the description is the checkbox's accessible description; banners
 *   always render in registry order), **Key metrics** and **Panels** (the eligible widgets
 *   NOT on the dashboard, grouped by `WIDGET_CATEGORY_ORDER`, each with its description,
 *   panels with their default width, and an "Add {title}" button). Widgets the viewer is not
 *   eligible for — including carried saved entries — are never listed.
 * - Add appends to the end of its zone (`editor.add`; panels at `defaultSize`, else Half).
 *   The gallery stays open, the row leaves the list, focus moves to the next Add button,
 *   else the previous one, else the gallery's Done, and the gallery's OWN polite region
 *   announces "{title} added at position n of n." (`aria-modal` hides the page's region).
 * - On close, `closeGallery` focuses the last-added widget's Move button (after the Modal's
 *   focus restore) and rings it briefly.
 * - Opened from an empty zone's placeholder ("Add key metrics" / "Add panels",
 *   `editor.galleryFocusZone`), focus lands on that section's heading (scrolled to the top of
 *   the panel) after the Modal's own initial focus; from "Add widgets" it starts at the top.
 * - Static cards only: the gallery renders no widget and fetches nothing.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { Button, Modal } from '../../ui';
import { isWidgetEligible } from '../dashboardLayout';
import { DASHBOARD_REGISTRY, WIDGET_CATEGORY_ORDER, type WidgetRegistry } from '../widgetRegistry';
import {
  PANEL_SIZE_LABELS,
  isPanelSize,
  type DashboardWidgetDefinition,
  type WidgetEligibilityContext,
} from '../widgetTypes';
import { EDIT_IDS, galleryAddId, safeId } from './editorCopy';
import type { LayoutEditor } from './useLayoutEditor';

/** What the gallery reads from `useLayoutEditor()`. */
export type WidgetGalleryEditor = Pick<
  LayoutEditor,
  | 'active'
  | 'galleryOpen'
  | 'galleryFocusZone'
  | 'draft'
  | 'controlsDisabled'
  | 'closeGallery'
  | 'add'
  | 'setBannerVisible'
>;

export interface WidgetGalleryProps {
  editor: WidgetGalleryEditor;
  eligibility: WidgetEligibilityContext;
  registry?: WidgetRegistry;
}

type CatalogZone = 'kpi' | 'panel';

const SECTION_HEADINGS: Record<CatalogZone, string> = { kpi: 'Key metrics', panel: 'Panels' };

/** The id of a catalog section's heading (focused when the gallery opens on that section). */
export const gallerySectionHeadingId = (zone: CatalogZone) => `dashboard-gallery-${zone}-heading`;

export function WidgetGallery({ editor, eligibility, registry = DASHBOARD_REGISTRY }: WidgetGalleryProps) {
  const [announcement, setAnnouncement] = useState<{ text: string; key: number }>({ text: '', key: 0 });
  const draft = editor.draft;
  const open = editor.active && editor.galleryOpen && draft !== null;

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

  const available = useMemo(() => {
    const onDashboard = new Set<string>([...(draft?.kpis ?? []), ...(draft?.panels ?? []).map(panel => panel.id)]);
    const grouped = (zone: CatalogZone) =>
      WIDGET_CATEGORY_ORDER.map(category => ({
        category,
        widgets: eligible[zone].filter(
          definition => definition.category === category && !onDashboard.has(definition.id)
        ),
      })).filter(group => group.widgets.length > 0);
    return { kpi: grouped('kpi'), panel: grouped('panel') };
  }, [draft, eligible]);

  // A reopened gallery starts with an empty region (the last session's message is stale).
  useEffect(() => {
    if (!open) setAnnouncement(current => (current.text ? { text: '', key: current.key + 1 } : current));
  }, [open]);

  // Opened from a zone placeholder: land on that section. The Modal defers its own initial
  // focus by a tick from its (child, so earlier) effect; this timer is queued after it.
  const focusZone = open ? editor.galleryFocusZone : null;
  useEffect(() => {
    if (!focusZone) return;
    const timer = window.setTimeout(() => {
      const heading = document.getElementById(gallerySectionHeadingId(focusZone));
      if (!heading) return;
      heading.focus();
      if (typeof heading.scrollIntoView === 'function') heading.scrollIntoView({ block: 'start' });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [focusZone]);

  if (!open || !draft) return null;

  /** Every Add button, in render order (where focus goes after an add). */
  const addOrder = (['kpi', 'panel'] as const).flatMap(zone =>
    available[zone].flatMap(group => group.widgets.map(definition => definition.id))
  );

  const handleAdd = (definition: DashboardWidgetDefinition) => {
    const position = addOrder.indexOf(definition.id);
    const next = addOrder[position + 1];
    const previous = position > 0 ? addOrder[position - 1] : undefined;
    const focusIds = [next, previous].filter((id): id is string => Boolean(id)).map(galleryAddId);
    const message = editor.add(definition, [...focusIds, EDIT_IDS.galleryDone]);
    if (message) setAnnouncement(current => ({ text: message, key: current.key + 1 }));
  };

  const renderZone = (zone: CatalogZone) => {
    const headingId = gallerySectionHeadingId(zone);
    const groups = available[zone];
    return (
      <section aria-labelledby={headingId} className="space-y-2">
        <h3 id={headingId} tabIndex={-1} className="text-sm font-semibold text-fd-ink">
          {SECTION_HEADINGS[zone]}
        </h3>
        {groups.length === 0 ? (
          <p className="border border-fd-line p-2 text-xs text-slate-400">Everything available is on your dashboard.</p>
        ) : (
          <div className="space-y-3 border border-fd-line p-2">
            {groups.map(({ category, widgets }) => {
              // Index, not the label: category labels contain spaces.
              const categoryId = `${headingId}-category-${WIDGET_CATEGORY_ORDER.indexOf(category)}`;
              return (
                <div key={category}>
                  <p id={categoryId} className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                    {category}
                  </p>
                  <ul aria-labelledby={categoryId} className="divide-y divide-fd-line">
                    {widgets.map(definition => (
                      <li key={definition.id} className="flex items-start gap-2 py-1.5">
                        <div className="min-w-0 flex-1">
                          <p data-testid="gallery-widget-title" className="break-words text-sm text-slate-200">
                            {definition.title}
                          </p>
                          <p className="text-xs text-slate-400">{definition.description}</p>
                          {zone === 'panel' && (
                            <p className="text-[10px] uppercase tracking-wide text-slate-400">
                              Default width:{' '}
                              {PANEL_SIZE_LABELS[isPanelSize(definition.defaultSize) ? definition.defaultSize : 'half']}
                            </p>
                          )}
                        </div>
                        <Button
                          id={galleryAddId(definition.id)}
                          variant="secondary"
                          size="sm"
                          className="flex-shrink-0"
                          aria-label={`Add ${definition.title}`}
                          disabled={editor.controlsDisabled}
                          onClick={() => handleAdd(definition)}
                        >
                          Add
                        </Button>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        )}
      </section>
    );
  };

  const bannerHeadingId = 'dashboard-gallery-banner-heading';
  return (
    <Modal open onClose={editor.closeGallery} size="3xl" ariaLabelledBy={EDIT_IDS.galleryHeading}>
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 id={EDIT_IDS.galleryHeading} className="text-lg font-semibold text-fd-ink">
              Add widgets
            </h2>
            <p className="text-sm text-slate-400">
              New widgets go to the end of their section; drag them or use their Move button to put them in place.
            </p>
          </div>
          <button
            type="button"
            className="btn-ghost btn-sm !px-1"
            aria-label="Close add widgets"
            onClick={editor.closeGallery}
          >
            <XMarkIcon aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>

        <section aria-labelledby={bannerHeadingId} className="space-y-2">
          <h3 id={bannerHeadingId} className="text-sm font-semibold text-fd-ink">
            Alerts &amp; banners
          </h3>
          <ul className="divide-y divide-fd-line border border-fd-line px-2">
            {eligible.banner.map(definition => {
              const checkboxId = `dashboard-gallery-banner-${safeId(definition.id)}`;
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
                    disabled={editor.controlsDisabled}
                    onChange={event => editor.setBannerVisible(definition.id, event.target.checked)}
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

        {renderZone('kpi')}
        {renderZone('panel')}

        <p role="status" aria-live="polite" className="sr-only" data-testid="gallery-announcement">
          <span key={announcement.key}>{announcement.text}</span>
        </p>

        <div className="flex justify-end border-t border-fd-line pt-3">
          <Button id={EDIT_IDS.galleryDone} onClick={editor.closeGallery}>
            Done
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export default WidgetGallery;
