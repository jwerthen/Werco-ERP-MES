/**
 * Test harness for dashboard edit mode WITHOUT the page: a controllable stand-in for
 * `useDashboardLayout` plus a miniature dashboard (header Customize button, polite region,
 * edit bar, KPI and panel zones of real `WidgetCell`s, the gallery) wired to the REAL
 * `useLayoutEditor`. Focus, announcements and the bar / gallery / menu behave as on the page.
 *
 * - `createFakeLayout()` mirrors `useDashboardLayout`'s result. Its `save` / `reset` / `reload`
 *   are `jest.fn`s whose default implementations behave like the server (a save stores the
 *   layout and bumps the version, reset returns to the default, reload answers the current
 *   response); `set()` changes what the layout shows (wrap it in `act`). Keep overrides on
 *   the real contract: `save` / `reset` REJECT with the server's error, but `reload` never
 *   rejects — a failed read resolves `null` (`reload.mockResolvedValue(null)`).
 * - `useFakeLayout(controller)` turns a controller into a `UseDashboardLayoutResult`.
 * - `<EditorHarness>` renders the miniature dashboard; `editorRef` exposes the hook's result.
 *   Widgets render as stubs (`data-testid="widget-<id>"`); `components` overrides any.
 */
import React, { useMemo, useState, useSyncExternalStore, type ComponentType } from 'react';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../types/dashboardLayout';
import { resolveLayout } from '../components/dashboard/dashboardLayout';
import type { DashboardLayoutStatus, UseDashboardLayoutResult } from '../components/dashboard/useDashboardLayout';
import { DASHBOARD_REGISTRY, type WidgetRegistry } from '../components/dashboard/widgetRegistry';
import type { DashboardWidgetDefinition, WidgetEligibilityContext } from '../components/dashboard/widgetTypes';
import { WidgetCell } from '../components/dashboard/WidgetCell';
import { createDragBridge, type DragBridge } from '../components/dashboard/editor/dragBridge';
import { EDIT_IDS, ZONE_LIST_LABELS } from '../components/dashboard/editor/editorCopy';
import { EditBar } from '../components/dashboard/editor/EditBar';
import { WidgetGallery } from '../components/dashboard/editor/WidgetGallery';
import { useLayoutEditor, type LayoutEditor, type ToastKind } from '../components/dashboard/editor/useLayoutEditor';

export interface FakeLayoutSnapshot {
  status: DashboardLayoutStatus;
  response: DashboardLayoutResponse | null;
}

export interface FakeLayoutController {
  getSnapshot(): FakeLayoutSnapshot;
  subscribe(listener: () => void): () => void;
  /** Change what the layout shows (call inside `act`). */
  set(next: Partial<FakeLayoutSnapshot>): void;
  save: jest.Mock<Promise<DashboardLayoutResponse>, [DashboardLayoutValues]>;
  reset: jest.Mock<Promise<DashboardLayoutResponse>, []>;
  reload: jest.Mock<Promise<DashboardLayoutResponse | null>, []>;
}

export interface FakeLayoutInit {
  /** The saved layout; null = the stock default. */
  saved?: DashboardLayoutValues | null;
  canEdit?: boolean;
  status?: DashboardLayoutStatus;
  version?: number;
  companyId?: number;
}

export function layoutResponse(
  layout: DashboardLayoutValues | null,
  overrides: Partial<DashboardLayoutResponse> = {}
): DashboardLayoutResponse {
  return { company_id: 1, version: 3, layout, updated_at: '2026-11-11T15:00:00Z', can_edit: true, ...overrides };
}

export function createFakeLayout(init: FakeLayoutInit = {}): FakeLayoutController {
  const status = init.status ?? 'ready';
  let snapshot: FakeLayoutSnapshot = {
    status,
    response:
      status === 'ready'
        ? layoutResponse(init.saved ?? null, {
            can_edit: init.canEdit ?? true,
            version: init.version ?? 3,
            company_id: init.companyId ?? 1,
          })
        : null,
  };
  const listeners = new Set<() => void>();
  const controller: FakeLayoutController = {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    set(next) {
      snapshot = { ...snapshot, ...next };
      Array.from(listeners).forEach(listener => listener());
    },
    save: jest.fn(async (layout: DashboardLayoutValues) => {
      const current = snapshot.response ?? layoutResponse(null);
      const response = { ...current, layout, version: current.version + 1 };
      controller.set({ status: 'ready', response });
      return response;
    }),
    reset: jest.fn(async () => {
      const current = snapshot.response ?? layoutResponse(null);
      const response = { ...current, layout: null, version: current.version + 1 };
      controller.set({ status: 'ready', response });
      return response;
    }),
    reload: jest.fn(async () => snapshot.response),
  };
  return controller;
}

export function useFakeLayout(controller: FakeLayoutController): UseDashboardLayoutResult {
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  return useMemo(
    () => ({
      status: snapshot.status,
      response: snapshot.response,
      saved: snapshot.response?.layout ?? null,
      canEdit: snapshot.response?.can_edit === true,
      reload: controller.reload,
      save: controller.save,
      reset: controller.reset,
    }),
    [snapshot, controller]
  );
}

const stubCache = new Map<string, ComponentType>();

/** A stable stub renderer per widget (stable identity, so the memoized body is honest). */
export function stubWidget(definition: DashboardWidgetDefinition): ComponentType {
  let stub = stubCache.get(definition.id);
  if (!stub) {
    const title = definition.title;
    const id = definition.id;
    stub = function StubWidget() {
      return (
        <div data-testid={`widget-${id}`}>
          <a href={`#${id}`}>{title}</a>
        </div>
      );
    };
    stubCache.set(definition.id, stub);
  }
  return stub;
}

export interface EditorHarnessProps {
  layout: FakeLayoutController;
  eligibility: WidgetEligibilityContext;
  scope?: string | null;
  registry?: WidgetRegistry;
  showToast?: (type: ToastKind, message: string) => void;
  onBeforeChange?: () => void;
  /** Renderer overrides by widget id (e.g. one that throws). */
  components?: Readonly<Record<string, ComponentType>>;
  editorRef?: { current: LayoutEditor | null };
  bridge?: DragBridge;
}

export function EditorHarness({
  layout: controller,
  eligibility,
  scope = 'scope-a',
  registry = DASHBOARD_REGISTRY,
  showToast = () => undefined,
  onBeforeChange,
  components,
  editorRef,
  bridge: givenBridge,
}: EditorHarnessProps) {
  const layout = useFakeLayout(controller);
  const [announcement, setAnnouncement] = useState({ text: '', key: 0 });
  const editor = useLayoutEditor({
    scope,
    layout,
    eligibility,
    registry,
    onBeforeChange,
    showToast,
    announce: text => setAnnouncement(current => ({ text, key: current.key + 1 })),
  });
  if (editorRef) editorRef.current = editor;
  const [ownBridge] = useState(createDragBridge);
  const bridge = givenBridge ?? ownBridge;
  const resolved = useMemo(
    () => resolveLayout(layout.saved, eligibility, registry),
    [layout.saved, eligibility, registry]
  );
  const view = editor.shown ?? resolved;
  const editing = editor.active;
  const componentFor = (definition: DashboardWidgetDefinition) => components?.[definition.id] ?? stubWidget(definition);

  return (
    <div>
      <button
        id={EDIT_IDS.customize}
        type="button"
        disabled={editing || layout.status === 'loading'}
        onClick={() => editor.enter()}
      >
        Customize
      </button>
      <p role="status" aria-live="polite" data-testid="page-announcer">
        <span key={announcement.key}>{announcement.text}</span>
      </p>
      <EditBar editor={editor} />
      <main id={EDIT_IDS.mainContent} tabIndex={-1}>
        <div data-testid="banner-zone">
          {view.banners.map(definition => (
            <div key={definition.id} data-testid={`banner-${definition.id}`}>
              {definition.title}
            </div>
          ))}
        </div>
        <div
          data-testid="kpi-zone"
          role={editing ? 'list' : undefined}
          aria-label={editing ? ZONE_LIST_LABELS.kpi : undefined}
        >
          {view.kpis.map((definition, index) => (
            <WidgetCell
              key={definition.id}
              definition={definition}
              component={componentFor(definition)}
              zone="kpi"
              edit={editing ? { editor, bridge, index, count: view.kpis.length } : null}
            />
          ))}
        </div>
        <div
          data-testid="panel-zone"
          role={editing ? 'list' : undefined}
          aria-label={editing ? ZONE_LIST_LABELS.panel : undefined}
        >
          {view.panels.map(({ definition, size }, index) => (
            <WidgetCell
              key={definition.id}
              definition={definition}
              component={componentFor(definition)}
              zone="panel"
              size={size}
              edit={editing ? { editor, bridge, index, count: view.panels.length } : null}
            />
          ))}
        </div>
      </main>
      <WidgetGallery editor={editor} eligibility={eligibility} registry={registry} />
    </div>
  );
}
