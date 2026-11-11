/**
 * Customize dashboard dialog: two-list editing with focus kept on the moved row, panel
 * widths, dirty tracking, carried (ineligible) widgets, the unsaved-changes guard on
 * every close path, non-optimistic save/reset, 409 recovery and the read-only session.
 */
import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import type { DashboardLayoutValues } from '../../types/dashboardLayout';
import { CLASSIC_LAYOUT_VALUES } from './widgetRegistry';
import type { Permission } from '../../utils/permissions';
import {
  CustomizeDashboardDialog,
  READ_ONLY_LAYOUT_NOTICE,
  type CustomizeDashboardDialogProps,
} from './CustomizeDashboardDialog';
import { DashboardLayoutCompanyChangedError } from './useDashboardLayout';
import type { WidgetEligibilityContext } from './widgetTypes';

jest.mock('../../services/api', () => ({ __esModule: true, default: {} }));

const ctxFor = (permissions: Permission[] | 'all', isAdmin = false): WidgetEligibilityContext => ({
  can: permission => permissions === 'all' || permissions.includes(permission),
  hasRole: () => isAdmin,
  isAdmin,
});
const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'receiving:view', 'shipping:view']);
const SHIPPING = ctxFor(['work_orders:view', 'inventory:view', 'shipping:view']);

const httpError = (status: number, detail: string) =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

function renderDialog(overrides: Partial<CustomizeDashboardDialogProps> = {}) {
  const props: CustomizeDashboardDialogProps = {
    saved: null,
    canEdit: true,
    eligibility: MANAGER,
    onSave: jest.fn().mockResolvedValue(undefined),
    onReset: jest.fn().mockResolvedValue(undefined),
    onReload: jest.fn().mockResolvedValue(null),
    onClose: jest.fn(),
    ...overrides,
  };
  const utils = render(<CustomizeDashboardDialog {...props} />);
  return { ...utils, props };
}

/** Let the Modal's deferred initial-focus timer run before we move focus ourselves. */
const settle = () =>
  act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
  });

const kpiSection = () => screen.getByRole('region', { name: 'Key metrics' });
const panelSection = () => screen.getByRole('region', { name: 'Panels' });
const onDashboardTitles = (section: HTMLElement) =>
  within(within(section).getByRole('list', { name: 'On your dashboard' }))
    .getAllByRole('listitem')
    .map(item => item.querySelector('[data-testid="widget-row-title"]')?.textContent);
/** The sr-only region that announces list edits. */
const liveRegion = () => document.querySelector('p.sr-only[aria-live="polite"]') as HTMLElement;
const saveButton = () => screen.getByRole('button', { name: 'Save layout' });
const reloadButton = () => screen.getByRole('button', { name: /Reload saved layout/ });
const dialogHeading = () => screen.getByRole('heading', { name: 'Customize dashboard', level: 2 });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => jest.restoreAllMocks());

it('lists the viewer’s widgets in render order and the rest as available', async () => {
  renderDialog();
  await settle();
  expect(screen.getByRole('dialog', { name: 'Customize dashboard' })).toBeInTheDocument();
  expect(onDashboardTitles(kpiSection())).toEqual([
    'Overdue',
    'Signed In, Idle',
    'Active Work Orders',
    'Signed In Now',
    'Checked In Now',
    'Due Today',
    'Calibration Due',
    'Low Stock Items',
    'Operations Completed Today',
    'Open NCRs',
  ]);
  expect(within(kpiSection()).getByRole('button', { name: 'Add Open CARs' })).toBeInTheDocument();
  expect(onDashboardTitles(panelSection())).toEqual([
    'Capacity Overview',
    'Live Shop Activity',
    'Work Center Status',
    'Signed In Right Now',
    'Recent Completions',
  ]);
  // The admin-only setup nudge is not offered to a manager.
  expect(screen.queryByRole('checkbox', { name: /Setup progress/ })).not.toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: /Needs attention/ })).toBeChecked();
  expect(saveButton()).toBeDisabled();
});

it('names each banner checkbox by its title and describes it by its description', async () => {
  renderDialog();
  await settle();
  const attention = screen.getByRole('checkbox', { name: 'Needs attention' });
  expect(attention).toHaveAccessibleName('Needs attention');
  expect(attention).toHaveAccessibleDescription(
    'The most urgent exception (overdue work, open NCRs, calibration, low stock) with a link to review it.'
  );
});

it.each([
  ['no saved layout', null],
  ['a saved layout that only carries the stock default', CLASSIC_LAYOUT_VALUES],
])('never tells a non-admin a saved widget is hidden when they have %s', async (_label, saved) => {
  renderDialog({ saved, eligibility: MANAGER });
  await settle();
  expect(screen.queryByText(/not available to you right now/)).not.toBeInTheDocument();
});

it('keeps focus on the moved row and announces its new position', async () => {
  renderDialog();
  await settle();
  const section = kpiSection();

  fireEvent.click(within(section).getByRole('button', { name: 'Move Overdue down' }));
  expect(onDashboardTitles(section).slice(0, 2)).toEqual(['Signed In, Idle', 'Overdue']);
  expect(document.activeElement).toBe(within(section).getByRole('button', { name: 'Move Overdue down' }));
  expect(liveRegion()).toHaveTextContent('Overdue moved to position 2 of 10');

  // Reaching the top disables "up", so focus moves to the row's "down" arrow.
  fireEvent.click(within(section).getByRole('button', { name: 'Move Overdue up' }));
  expect(document.activeElement).toBe(within(section).getByRole('button', { name: 'Move Overdue down' }));
  expect(within(section).getByRole('button', { name: 'Move Overdue up' })).toBeDisabled();
  expect(liveRegion()).toHaveTextContent('Overdue moved to position 1 of 10');

  // Reaching the bottom disables "down", so focus moves to the row's "up" arrow.
  fireEvent.click(within(section).getByRole('button', { name: 'Move Operations Completed Today down' }));
  expect(document.activeElement).toBe(
    within(section).getByRole('button', { name: 'Move Operations Completed Today up' })
  );
  expect(saveButton()).toBeEnabled();
});

it('adds to the end and removes, moving focus to a control that still exists', async () => {
  renderDialog();
  await settle();
  const section = kpiSection();

  fireEvent.click(within(section).getByRole('button', { name: 'Add Open CARs' }));
  expect(onDashboardTitles(section).at(-1)).toBe('Open CARs');
  expect(document.activeElement).toBe(within(section).getByRole('button', { name: 'Move Open CARs up' }));
  expect(liveRegion()).toHaveTextContent('Open CARs added at position 11 of 11');

  fireEvent.click(within(section).getByRole('button', { name: 'Remove Overdue' }));
  expect(onDashboardTitles(section)[0]).toBe('Signed In, Idle');
  expect(document.activeElement).toBe(within(section).getByRole('button', { name: 'Remove Signed In, Idle' }));
  expect(within(section).getByRole('button', { name: 'Add Overdue' })).toBeInTheDocument();
  expect(liveRegion()).toHaveTextContent('Overdue removed from your dashboard');
});

it('saves the edited layout, including a changed panel width', async () => {
  const { props } = renderDialog();
  await settle();
  fireEvent.change(screen.getByRole('combobox', { name: 'Width of Capacity Overview' }), {
    target: { value: 'full' },
  });
  fireEvent.click(screen.getByRole('checkbox', { name: /Morning brief/ }));
  await act(async () => {
    fireEvent.click(saveButton());
  });
  expect(props.onSave).toHaveBeenCalledTimes(1);
  const payload = (props.onSave as jest.Mock).mock.calls[0][0] as DashboardLayoutValues;
  expect(payload.format_version).toBe(1);
  expect(payload.widgets).toContainEqual({ id: 'capacity_overview', size: 'full' });
  expect(payload.widgets.map(widget => widget.id)).not.toContain('morning_brief');
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

it('offers the opt-in panels the viewer may see, grouped by category, and adds one at its default width', async () => {
  const { props } = renderDialog();
  await settle();
  const available = within(panelSection()).getByRole('group', { name: 'Available' });
  const categoryOf = (title: string) =>
    within(available)
      .getByRole('button', { name: `Add ${title}` })
      .closest('ul')?.previousElementSibling?.textContent;

  // Category groups follow WIDGET_CATEGORY_ORDER; only categories with an offer render.
  const categories = Array.from(available.querySelectorAll(':scope > div > p')).map(label => label.textContent);
  expect(categories).toEqual(['Alerts', 'Shop floor', 'Work orders', 'Quality', 'Supply chain']);
  expect(categoryOf('Action Inbox')).toBe('Alerts');
  expect(categoryOf('Late Jobs')).toBe('Work orders');
  expect(categoryOf('Active Downtime')).toBe('Shop floor');
  expect(categoryOf('Quality Pulse')).toBe('Quality');
  expect(categoryOf('Inspection Queue')).toBe('Supply chain');
  // Each category's list is named by its sub-heading, so the grouping is not visual only.
  const shopFloor = within(available).getByRole('list', { name: 'Shop floor' });
  expect(within(shopFloor).getByRole('button', { name: 'Add Active Downtime' })).toBeInTheDocument();
  expect(within(available).getByRole('list', { name: 'Quality' })).toContainElement(
    within(available).getByRole('button', { name: 'Add Quality Pulse' })
  );
  // require_role([ADMIN, MANAGER, SUPERVISOR]) panels are not offered when hasRole says no.
  expect(within(available).queryByRole('button', { name: 'Add Delivery & Yield KPIs' })).not.toBeInTheDocument();
  expect(within(available).queryByRole('button', { name: 'Add Visitors On Site' })).not.toBeInTheDocument();

  fireEvent.click(within(available).getByRole('button', { name: 'Add Quality Pulse' }));
  expect(onDashboardTitles(panelSection()).at(-1)).toBe('Quality Pulse');
  expect(screen.getByRole('combobox', { name: 'Width of Quality Pulse' })).toHaveValue('half');
  await act(async () => {
    fireEvent.click(saveButton());
  });
  const payload = (props.onSave as jest.Mock).mock.calls[0][0] as DashboardLayoutValues;
  expect(payload.widgets.at(-1)).toEqual({ id: 'quality_pulse', size: 'half' });
});

it('carries saved widgets the viewer cannot see through a save', async () => {
  const saved: DashboardLayoutValues = {
    format_version: 1,
    widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.open_cars' }, { id: 'kpi.due_today' }],
  };
  const { props } = renderDialog({ saved, eligibility: SHIPPING });
  await settle();
  expect(screen.queryByText('Open CARs')).not.toBeInTheDocument();
  expect(screen.getByText(/1 widget in your saved layout is not available to you right now/)).toBeInTheDocument();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Due Today' }));
  await act(async () => {
    fireEvent.click(saveButton());
  });
  const payload = (props.onSave as jest.Mock).mock.calls[0][0] as DashboardLayoutValues;
  expect(payload.widgets.map(widget => widget.id)).toEqual(['kpi.overdue', 'kpi.open_cars']);
});

it('closes without asking when nothing changed, and asks before discarding edits on Escape', async () => {
  const clean = renderDialog();
  await settle();
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  fireEvent.keyDown(document.body, { key: 'Escape' });
  expect(confirm).not.toHaveBeenCalled();
  expect(clean.props.onClose).toHaveBeenCalledTimes(1);
  clean.unmount();

  const dirty = renderDialog();
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  fireEvent.keyDown(document.body, { key: 'Escape' });
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(dirty.props.onClose).not.toHaveBeenCalled();

  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(dirty.props.onClose).toHaveBeenCalledTimes(1);
});

it('cannot be dismissed while a save is in flight', async () => {
  let finish!: () => void;
  const onSave = jest.fn(
    () =>
      new Promise<void>(resolve => {
        finish = resolve;
      })
  );
  const { props } = renderDialog({ onSave });
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  fireEvent.click(saveButton());
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  fireEvent.keyDown(document.body, { key: 'Escape' });
  expect(props.onClose).not.toHaveBeenCalled();
  await act(async () => finish());
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

it('shows a 409 inline, focuses the recovery action and replaces the draft with the reloaded layout', async () => {
  const reloaded: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.due_today' }] };
  const onSave = jest
    .fn()
    .mockRejectedValue(httpError(409, 'Your dashboard layout changed in another tab. Reload it before saving.'));
  const pendingReload = deferred<DashboardLayoutValues | null>();
  const onReload = jest.fn(() => pendingReload.promise);
  const { props } = renderDialog({ onSave, onReload });
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  saveButton().focus();
  await act(async () => {
    fireEvent.click(saveButton());
  });
  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent('Your dashboard layout changed in another tab. Reload it before saving.');
  expect(saveButton()).toBeDisabled();
  expect(props.onClose).not.toHaveBeenCalled();
  // Focus leaves the now-disabled Save button for the recovery action.
  expect(reloadButton()).toHaveFocus();

  fireEvent.click(reloadButton());
  // While the reload runs the alert and its button stay mounted, showing the spinner.
  expect(onReload).toHaveBeenCalledTimes(1);
  expect(within(screen.getByRole('alert')).getByRole('button', { name: /Reload saved layout/ })).toBeDisabled();
  expect(reloadButton().querySelector('.animate-spin')).not.toBeNull();

  await act(async () => pendingReload.resolve(reloaded));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  // The discard is announced: the notice is written into a live region that was already mounted.
  const notice = screen.getByText('Reloaded your saved layout. Your unsaved changes were discarded.');
  expect(notice.closest('[aria-live="polite"]')).not.toBeNull();
  expect(dialogHeading()).toHaveFocus();
  expect(onDashboardTitles(kpiSection())).toEqual(['Due Today']);
});

it('keeps the recovery action, focused, when the reload itself fails', async () => {
  const onSave = jest.fn().mockRejectedValue(httpError(409, 'Changed in another tab.'));
  const onReload = jest.fn().mockRejectedValue(httpError(503, 'Service unavailable. Try again.'));
  renderDialog({ onSave, onReload });
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  await act(async () => {
    fireEvent.click(saveButton());
  });
  await act(async () => {
    fireEvent.click(reloadButton());
  });
  expect(screen.getByRole('alert')).toHaveTextContent('Service unavailable. Try again.');
  expect(reloadButton()).toBeEnabled();
  expect(reloadButton()).toHaveFocus();
});

it('resets to the default through a confirmation', async () => {
  const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
  const { props } = renderDialog({ saved });
  await settle();
  fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }));
  const confirm = screen.getByRole('dialog', { name: 'Reset dashboard to default?' });
  await act(async () => {
    fireEvent.click(within(confirm).getByRole('button', { name: 'Reset to default' }));
  });
  expect(props.onReset).toHaveBeenCalledTimes(1);
  expect(props.onSave).not.toHaveBeenCalled();
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

it('moves focus to the recovery action when a reset fails with 409 (Reset itself is now disabled)', async () => {
  const saved: DashboardLayoutValues = { format_version: 1, widgets: [{ id: 'kpi.overdue' }] };
  const onReset = jest.fn().mockRejectedValue(httpError(409, 'Your dashboard layout changed in another tab.'));
  renderDialog({ saved, onReset });
  await settle();
  const reset = screen.getByRole('button', { name: 'Reset to default' });
  reset.focus();
  fireEvent.click(reset);
  const confirm = screen.getByRole('dialog', { name: 'Reset dashboard to default?' });
  await settle();
  await act(async () => {
    fireEvent.click(within(confirm).getByRole('button', { name: 'Reset to default' }));
  });
  expect(screen.queryByRole('dialog', { name: 'Reset dashboard to default?' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reset to default' })).toBeDisabled();
  expect(reloadButton()).toHaveFocus();
  expect(document.activeElement).not.toBe(document.body);
});

it('uses AA-contrast text for its small labels (no slate-500 text on the panel)', async () => {
  renderDialog();
  await settle();
  const dialog = screen.getByRole('dialog', { name: 'Customize dashboard' });
  expect(dialog.querySelectorAll('[class*="text-slate-500"]')).toHaveLength(0);
});

it('lets a panel name wrap with its controls below it instead of squeezing it to an ellipsis', async () => {
  renderDialog();
  await settle();
  const list = within(panelSection()).getByRole('list', { name: 'On your dashboard' });
  const row = within(list).getAllByRole('listitem')[0];
  const name = within(row).getByTestId('widget-row-title');
  expect(name).toHaveTextContent('Capacity Overview');
  // A real flex basis (not basis-0) is what makes the controls wrap below the name, and
  // without `truncate` a long name wraps rather than collapsing to "…".
  expect(name).not.toHaveClass('truncate');
  expect(name).toHaveClass('basis-40');
  const controls = within(row).getByRole('combobox', { name: 'Width of Capacity Overview' }).parentElement;
  expect(controls).not.toBe(row);
  expect(controls).toContainElement(within(row).getByRole('button', { name: 'Remove Capacity Overview' }));
});

it('treats a save answered for another company like a 409: says so, stops saving and focuses the recovery action', async () => {
  const onSave = jest.fn().mockRejectedValue(new DashboardLayoutCompanyChangedError());
  const { props } = renderDialog({ onSave });
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  saveButton().focus();
  await act(async () => {
    fireEvent.click(saveButton());
  });
  expect(screen.getByRole('alert')).toHaveTextContent(
    'Your active company changed. Reload the dashboard before saving its layout.'
  );
  expect(saveButton()).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Reset to default' })).toBeDisabled();
  expect(within(kpiSection()).getByRole('button', { name: 'Add Overdue' })).toBeDisabled();
  expect(reloadButton()).toHaveFocus();
  expect(props.onClose).not.toHaveBeenCalled();
});

it('keeps the draft editable and lets the user retry after a failure that is not a conflict', async () => {
  const onSave = jest
    .fn()
    .mockRejectedValueOnce(
      httpError(
        503,
        'Your dashboard layout could not be saved because its audit record failed; nothing was changed. Retry.'
      )
    )
    .mockResolvedValue(undefined);
  const { props } = renderDialog({ onSave });
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  await act(async () => {
    fireEvent.click(saveButton());
  });
  expect(screen.getByRole('alert')).toHaveTextContent(/audit record failed; nothing was changed/);
  // No reload is forced: the server refused nothing about the version, so the edits stand.
  expect(screen.queryByRole('button', { name: /Reload saved layout/ })).not.toBeInTheDocument();
  expect(onDashboardTitles(kpiSection())).not.toContain('Overdue');
  expect(saveButton()).toBeEnabled();
  expect(props.onClose).not.toHaveBeenCalled();

  await act(async () => {
    fireEvent.click(saveButton());
  });
  expect(onSave).toHaveBeenCalledTimes(2);
  expect(onSave.mock.calls[1][0]).toEqual(onSave.mock.calls[0][0]);
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

it('offers no reset while the dashboard already shows the default', async () => {
  renderDialog({ saved: null });
  await settle();
  const reset = screen.getByRole('button', { name: 'Reset to default' });
  expect(reset).toBeDisabled();
  expect(reset).toHaveAttribute('title', 'You are already using the default layout');
});

it('asks before the close button discards edits, like Escape and Cancel', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  const { props } = renderDialog();
  await settle();
  fireEvent.click(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' }));
  fireEvent.click(screen.getByRole('button', { name: 'Close customize dashboard' }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(props.onClose).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: 'Close customize dashboard' }));
  expect(props.onClose).toHaveBeenCalledTimes(1);
});

it('is clean again once a banner is toggled off and back on (banners keep registry order)', async () => {
  const { props } = renderDialog();
  await settle();
  const attention = screen.getByRole('checkbox', { name: 'Needs attention' });
  fireEvent.click(attention);
  expect(attention).not.toBeChecked();
  expect(saveButton()).toBeEnabled();
  fireEvent.click(attention);
  expect(attention).toBeChecked();
  expect(saveButton()).toBeDisabled();

  // Toggled back on, it still saves in registry order, not at the end of the banner list.
  fireEvent.click(screen.getByRole('checkbox', { name: 'Alert chips' }));
  fireEvent.click(attention);
  fireEvent.click(attention);
  await act(async () => {
    fireEvent.click(saveButton());
  });
  const payload = (props.onSave as jest.Mock).mock.calls[0][0] as DashboardLayoutValues;
  const ids = payload.widgets.map(widget => widget.id);
  expect(ids.slice(0, 3)).toEqual(['attention', 'setup_nudge', 'morning_brief']);
  expect(ids).not.toContain('alert_chips');
});

it('is read-only for a session that cannot save', async () => {
  renderDialog({ canEdit: false, saved: { format_version: 1, widgets: [{ id: 'kpi.overdue' }] } });
  await settle();
  expect(screen.getByText(READ_ONLY_LAYOUT_NOTICE)).toBeInTheDocument();
  expect(saveButton()).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Reset to default' })).toBeDisabled();
  expect(within(kpiSection()).getByRole('button', { name: 'Remove Overdue' })).toBeDisabled();
  expect(within(kpiSection()).getByRole('button', { name: 'Add Due Today' })).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: /Needs attention/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
});
