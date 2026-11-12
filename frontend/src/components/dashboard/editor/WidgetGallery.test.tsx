/**
 * The "Add widgets" gallery on a miniature dashboard wired to the real editor: only eligible
 * widgets not already shown, grouped by category with descriptions and default widths; the
 * banner checkboxes; Add (end of zone, default width, row leaves, focus next → previous →
 * Done, the gallery's own live region); closing without confirmation and the focus hand-off
 * to the last-added widget. Ports dialog cases #1 (available half), #2, #4 (add half), #6,
 * #14 and #15.
 */
import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import type { DashboardLayoutValues } from '../../../types/dashboardLayout';
import type { Permission } from '../../../utils/permissions';
import { createFakeLayout, EditorHarness, type FakeLayoutController } from '../../../test-utils/layoutEditorHarness';
import type { WidgetEligibilityContext } from '../widgetTypes';
import { ADDED_HIGHLIGHT_MS } from './useLayoutEditor';

jest.mock('../../../services/api', () => ({ __esModule: true, default: {} }));

const ctxFor = (permissions: Permission[], roles = false): WidgetEligibilityContext => ({
  can: permission => permissions.includes(permission),
  hasRole: () => roles,
  isAdmin: false,
});
const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'receiving:view', 'shipping:view']);
const SHIPPING = ctxFor(['work_orders:view', 'inventory:view', 'shipping:view']);

interface Options {
  layout?: FakeLayoutController;
  eligibility?: WidgetEligibilityContext;
}

function renderGallery({ layout = createFakeLayout(), eligibility = MANAGER }: Options = {}) {
  const utils = render(<EditorHarness layout={layout} eligibility={eligibility} />);
  fireEvent.click(screen.getByRole('button', { name: 'Customize' }));
  const addWidgets = within(screen.getByRole('region', { name: 'Customizing dashboard' })).getByRole('button', {
    name: 'Add widgets',
  });
  addWidgets.focus();
  fireEvent.click(addWidgets);
  return { ...utils, layout, addWidgets };
}

/** Let the Modal's deferred initial-focus timer and passive effects run. */
const settle = () =>
  act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
  });

const gallery = () => screen.getByRole('dialog', { name: 'Add widgets' });
const section = (name: string) => within(gallery()).getByRole('region', { name });
const addButton = (title: string) => within(gallery()).getByRole('button', { name: `Add ${title}` });
const galleryRegion = () => within(gallery()).getByTestId('gallery-announcement');
const cellIds = (zone: 'kpi' | 'panel') =>
  Array.from(screen.getByTestId(`${zone}-zone`).querySelectorAll('[data-widget-cell]')).map(cell =>
    cell.getAttribute('data-widget-cell')
  );

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('what it offers', () => {
  it('lists only eligible widgets that are not on the dashboard, grouped by category, with descriptions', async () => {
    renderGallery();
    await settle();
    // Every classic KPI is on the dashboard; the two quality tiles are offered.
    const kpis = section('Key metrics');
    expect(
      within(kpis)
        .getAllByTestId('gallery-widget-title')
        .map(title => title.textContent)
    ).toEqual(['Open CARs', 'Pending FAIs']);
    expect(within(kpis).queryByRole('button', { name: 'Add Overdue' })).not.toBeInTheDocument();
    expect(within(kpis).getByText('Corrective action requests not yet closed.')).toBeInTheDocument();

    const panels = section('Panels');
    // Category groups follow WIDGET_CATEGORY_ORDER; only categories with an offer render.
    const categories = Array.from(panels.querySelectorAll('ul')).map(list => list.previousElementSibling?.textContent);
    expect(categories).toEqual(['Alerts', 'Shop floor', 'Work orders', 'Quality', 'Supply chain']);
    const shopFloor = within(panels).getByRole('list', { name: 'Shop floor' });
    expect(within(shopFloor).getByRole('button', { name: 'Add Active Downtime' })).toBeInTheDocument();
    expect(within(panels).getByRole('list', { name: 'Quality' })).toContainElement(addButton('Quality Pulse'));
    expect(within(panels).getByRole('list', { name: 'Work orders' })).toContainElement(addButton('Late Jobs'));
    expect(within(panels).getByRole('list', { name: 'Alerts' })).toContainElement(addButton('Action Inbox'));
    expect(within(panels).getByRole('list', { name: 'Supply chain' })).toContainElement(addButton('Inspection Queue'));
    // require_role([...]) panels are not offered when hasRole says no.
    expect(within(panels).queryByRole('button', { name: 'Add Delivery & Yield KPIs' })).not.toBeInTheDocument();
    expect(within(panels).queryByRole('button', { name: 'Add Visitors On Site' })).not.toBeInTheDocument();
    // Panels show their default width.
    const pulseRow = addButton('Quality Pulse').closest('li')!;
    expect(within(pulseRow).getByText(/Default width:/)).toHaveTextContent('Default width: Half');
    // The admin-only setup nudge is not offered to a manager.
    expect(within(gallery()).queryByRole('checkbox', { name: 'Setup progress' })).not.toBeInTheDocument();
  });

  it('never lists a saved widget the viewer cannot see, nor one they are not eligible for', async () => {
    const saved: DashboardLayoutValues = {
      format_version: 1,
      widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.open_cars' }, { id: 'kpi.due_today' }],
    };
    renderGallery({ layout: createFakeLayout({ saved }), eligibility: SHIPPING });
    await settle();
    expect(within(gallery()).queryByText('Open CARs')).not.toBeInTheDocument();
    expect(within(gallery()).queryByText('Pending FAIs')).not.toBeInTheDocument();
    expect(within(gallery()).queryByRole('button', { name: 'Add Quality Pulse' })).not.toBeInTheDocument();
    expect(addButton('Signed In, Idle')).toBeInTheDocument();
  });

  it('names each banner checkbox by its title and describes it by its description', async () => {
    renderGallery();
    await settle();
    const attention = within(gallery()).getByRole('checkbox', { name: 'Needs attention' });
    expect(attention).toBeChecked();
    expect(attention).toHaveAccessibleName('Needs attention');
    expect(attention).toHaveAccessibleDescription(
      'The most urgent exception (overdue work, open NCRs, calibration, low stock) with a link to review it.'
    );
    fireEvent.click(attention);
    expect(attention).not.toBeChecked();
    expect(screen.queryByTestId('banner-attention')).not.toBeInTheDocument();
    fireEvent.click(attention);
    expect(screen.getByTestId('banner-attention')).toBeInTheDocument();
  });
});

describe('adding', () => {
  it('appends at the default width, drops the row, focuses the next Add and announces in its own region', async () => {
    renderGallery();
    await settle();
    fireEvent.click(addButton('Quality Pulse'));
    expect(cellIds('panel').at(-1)).toBe('quality_pulse');
    expect(document.querySelector('[data-widget-cell="quality_pulse"]')).toHaveClass('xl:col-span-6');
    expect(within(gallery()).queryByRole('button', { name: 'Add Quality Pulse' })).not.toBeInTheDocument();
    expect(addButton('Inspection Queue')).toHaveFocus();
    expect(galleryRegion()).toHaveTextContent('Quality Pulse added at position 6 of 6.');
    expect(galleryRegion()).toHaveAttribute('aria-live', 'polite');
    // The page's region (outside the aria-modal dialog) is not where it is said.
    expect(screen.getByTestId('page-announcer')).not.toHaveTextContent('Quality Pulse');
  });

  it('focuses the previous Add when the last one is used, then the gallery Done when none is left', async () => {
    renderGallery();
    await settle();
    fireEvent.click(addButton('Pending FAIs'));
    expect(cellIds('kpi').at(-1)).toBe('kpi.pending_fai');
    // Next in render order is the first panel offer.
    expect(addButton('Action Inbox')).toHaveFocus();

    const panelAdds = () => within(section('Panels')).queryAllByRole('button', { name: /^Add / });
    const last = panelAdds().at(-1)!;
    const beforeLast = panelAdds().at(-2)!;
    fireEvent.click(last);
    expect(beforeLast).toHaveFocus();

    // Use up every offer: focus ends on the gallery's Done.
    let remaining = within(gallery()).queryAllByRole('button', { name: /^Add / });
    while (remaining.length > 0) {
      fireEvent.click(remaining[0]);
      remaining = within(gallery()).queryAllByRole('button', { name: /^Add / });
    }
    expect(within(gallery()).getByRole('button', { name: 'Done' })).toHaveFocus();
    expect(within(section('Panels')).getByText('Everything available is on your dashboard.')).toBeInTheDocument();
  });

  it('lets a long title wrap with its Add button still beside it', async () => {
    renderGallery();
    await settle();
    const button = addButton('Quality Pulse');
    const row = button.closest('li')!;
    const title = within(row).getByTestId('gallery-widget-title');
    expect(title).toHaveClass('break-words');
    expect(title).not.toHaveClass('truncate');
    expect(title.parentElement).toHaveClass('min-w-0', 'flex-1');
    expect(button).toHaveClass('flex-shrink-0');
    expect(title.parentElement).not.toContainElement(button);
  });

  it('uses AA-contrast text for its small labels (no slate-500 text on the panel)', async () => {
    renderGallery();
    await settle();
    expect(gallery().querySelectorAll('[class*="text-slate-500"]')).toHaveLength(0);
  });
});

describe('closing', () => {
  it.each([
    ['Escape', () => fireEvent.keyDown(document.body, { key: 'Escape' })],
    ['the close button', () => fireEvent.click(within(gallery()).getByRole('button', { name: 'Close add widgets' }))],
    ['Done', () => fireEvent.click(within(gallery()).getByRole('button', { name: 'Done' }))],
    [
      'the backdrop',
      () => {
        const backdrop = gallery().parentElement!;
        fireEvent.click(backdrop);
      },
    ],
  ])('closes on %s without asking, even with changes, and keeps the additions', async (_how, close) => {
    const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
    renderGallery();
    await settle();
    fireEvent.click(addButton('Quality Pulse'));
    act(() => close());
    await settle();
    expect(screen.queryByRole('dialog', { name: 'Add widgets' })).not.toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    expect(cellIds('panel').at(-1)).toBe('quality_pulse');
  });

  it('hands focus to the last-added widget after the Modal restores its opener, and rings it briefly', async () => {
    renderGallery();
    await settle();
    fireEvent.click(addButton('Open CARs'));
    fireEvent.click(addButton('Quality Pulse'));
    fireEvent.click(within(gallery()).getByRole('button', { name: 'Done' }));
    await settle();
    const move = screen.getByRole('button', { name: 'Move or resize Quality Pulse' });
    expect(move).toHaveFocus();
    const cell = document.querySelector('[data-widget-cell="quality_pulse"]')!;
    expect(cell).toHaveClass('ring-2', 'ring-fd-blue');
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, ADDED_HIGHLIGHT_MS + 20));
    });
    expect(cell).not.toHaveClass('ring-2');
  });

  it('returns focus to Add widgets when nothing was added', async () => {
    const { addWidgets } = renderGallery();
    await settle();
    fireEvent.click(within(gallery()).getByRole('button', { name: 'Done' }));
    await settle();
    expect(addWidgets).toHaveFocus();
  });

  it('reopens with an empty announcement region (the last visit’s "added" message is stale)', async () => {
    const { addWidgets } = renderGallery();
    await settle();
    fireEvent.click(addButton('Quality Pulse'));
    expect(galleryRegion()).toHaveTextContent('Quality Pulse added at position 6 of 6.');
    fireEvent.click(within(gallery()).getByRole('button', { name: 'Done' }));
    await settle();
    expect(screen.queryByRole('dialog', { name: 'Add widgets' })).not.toBeInTheDocument();

    // Empty from the reopened dialog's first frame, not only after an effect has run: the
    // region is read at the moment React inserts the dialog into the document.
    const insertedRegionTexts = recordGalleryRegionOnInsert();
    fireEvent.click(addWidgets);
    expect(insertedRegionTexts).toEqual(['']);
    await settle();
    expect(galleryRegion()).toHaveTextContent(/^$/);
  });
});

/**
 * The gallery live region's text each time a subtree holding it enters the document, read
 * synchronously inside the DOM insertion. React builds the dialog off-document and inserts it
 * during its commit, before any effect runs, so this is what the reopened dialog first showed
 * (a live region that appears with text may be read out at once).
 */
function recordGalleryRegionOnInsert(): string[] {
  const texts: string[] = [];
  const selector = '[data-testid="gallery-announcement"]';
  const record = (parent: Node, node: Node) => {
    if (!parent.isConnected || !(node instanceof Element)) return;
    const region = node.matches(selector) ? node : node.querySelector(selector);
    if (region) texts.push(region.textContent ?? '');
  };
  const { appendChild, insertBefore } = Node.prototype;
  jest.spyOn(Node.prototype, 'appendChild').mockImplementation(function (this: Node, node: Node) {
    record(this, node);
    return appendChild.call(this, node);
  });
  jest.spyOn(Node.prototype, 'insertBefore').mockImplementation(function (this: Node, node: Node, child: Node | null) {
    record(this, node);
    return insertBefore.call(this, node, child);
  });
  return texts;
}
