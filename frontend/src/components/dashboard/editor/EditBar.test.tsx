/**
 * The edit bar on a miniature dashboard wired to the real editor: entering, the in-flight
 * lock, 409 / company-changed recovery with a focused Reload, reload success and failure,
 * Reset through its confirmation (and its failures), Undo's aria-disabled behaviour, the
 * carried note, Done / Cancel focus, and AA contrast. Ports dialog cases #9–#14, #16–#18.
 */
import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../../../types/dashboardLayout';
import type { Permission } from '../../../utils/permissions';
import {
  createFakeLayout,
  EditorHarness,
  layoutResponse,
  type FakeLayoutController,
} from '../../../test-utils/layoutEditorHarness';
import { DashboardLayoutCompanyChangedError } from '../useDashboardLayout';
import type { WidgetEligibilityContext } from '../widgetTypes';
import * as copy from './editorCopy';

jest.mock('../../../services/api', () => ({ __esModule: true, default: {} }));

const ctxFor = (permissions: Permission[]): WidgetEligibilityContext => ({
  can: permission => permissions.includes(permission),
  hasRole: () => false,
  isAdmin: false,
});
const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'receiving:view', 'shipping:view']);
const SHIPPING = ctxFor(['work_orders:view', 'inventory:view', 'shipping:view']);
const SAVED: DashboardLayoutValues = {
  format_version: 1,
  widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.due_today' }, { id: 'capacity_overview', size: 'wide' }],
};

const httpError = (status: number, detail: string) =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}

interface Options {
  layout?: FakeLayoutController;
  eligibility?: WidgetEligibilityContext;
}

function renderEditing({ layout = createFakeLayout({ saved: SAVED }), eligibility = MANAGER }: Options = {}) {
  const showToast = jest.fn();
  const utils = render(<EditorHarness layout={layout} eligibility={eligibility} showToast={showToast} />);
  fireEvent.click(customize());
  return { ...utils, layout, showToast };
}

/** Let the Modal's deferred initial-focus timer and passive effects run. */
const settle = () =>
  act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
  });

const customize = () => screen.getByRole('button', { name: 'Customize' });
const bar = () => screen.getByRole('region', { name: 'Customizing dashboard' });
const inBar = (name: string | RegExp) => within(bar()).getByRole('button', { name });
const done = () => inBar(/Done/);
const reloadButton = () => inBar(/Reload saved layout/);
const announcer = () => screen.getByTestId('page-announcer');
const kpiCells = () =>
  Array.from(screen.getByTestId('kpi-zone').querySelectorAll('[data-widget-cell]')).map(cell =>
    cell.getAttribute('data-widget-cell')
  );

afterEach(() => jest.restoreAllMocks());

describe('entering and leaving', () => {
  it('focuses the bar heading, disables Customize and describes every Move button with the shared help', () => {
    renderEditing();
    expect(screen.getByRole('heading', { name: 'Customizing dashboard', level: 2 })).toHaveFocus();
    expect(customize()).toBeDisabled();
    expect(announcer()).toHaveTextContent(copy.ENTER_ANNOUNCEMENT);
    const help = document.getElementById(copy.EDIT_IDS.moveHelp)!;
    expect(help).not.toBeVisible();
    expect(help).toHaveTextContent(copy.MOVE_HELP);
    expect(screen.getByRole('button', { name: 'Move Overdue' })).toHaveAccessibleDescription(
      `${copy.MOVE_HELP} Position 1 of 2.`
    );
    // The bar's controls, in tab order.
    expect(
      within(bar())
        .getAllByRole('button')
        .map(button => button.textContent?.trim())
    ).toEqual(['Add widgets', 'Undo', 'Reset to default', 'Cancel', 'Done']);
  });

  it('renders nothing outside edit mode', () => {
    render(<EditorHarness layout={createFakeLayout()} eligibility={MANAGER} />);
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
  });

  it('Done with nothing changed leaves without a request and focuses Customize', () => {
    const { layout } = renderEditing();
    fireEvent.click(done());
    expect(layout.save).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(customize()).toHaveFocus();
    expect(announcer()).toHaveTextContent(copy.DONE_UNCHANGED_ANNOUNCEMENT);
  });

  it('Done saves, toasts and focuses Customize; the unsaved chip tracks the draft', async () => {
    const { layout, showToast } = renderEditing();
    expect(within(bar()).queryByText('Unsaved changes')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    expect(within(bar()).getByText('Unsaved changes')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(done());
    });
    expect(layout.save).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith('success', copy.SAVED_TOAST);
    expect(customize()).toHaveFocus();
    expect(kpiCells()).toEqual(['kpi.due_today']);
  });

  it('Cancel asks before discarding, and leaves to Customize with the announcement', () => {
    const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
    renderEditing();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    fireEvent.click(inBar('Cancel'));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(bar()).toBeInTheDocument();
    confirm.mockReturnValue(true);
    fireEvent.click(inBar('Cancel'));
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(kpiCells()).toEqual(['kpi.overdue', 'kpi.due_today']);
    expect(customize()).toHaveFocus();
    expect(announcer()).toHaveTextContent(copy.CANCELLED_ANNOUNCEMENT);
  });
});

describe('the heading and the notes', () => {
  it('keeps the focused heading visible at every width (entry and a reload focus it)', () => {
    renderEditing();
    const heading = screen.getByRole('heading', { name: 'Customizing dashboard', level: 2 });
    expect(heading).toHaveFocus();
    expect(heading.className).not.toMatch(/sr-only/);
  });

  it('warns before Done when the saved layout names widgets this version of the app does not know', () => {
    const saved: DashboardLayoutValues = {
      format_version: 1,
      widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.from_a_newer_bundle' }, { id: 'kpi.from_a_newer_bundle' }],
    };
    renderEditing({ layout: createFakeLayout({ saved }) });
    expect(within(bar()).getByText(copy.unknownWidgetsNotice(1))).toBeInTheDocument();
  });

  it('shows no such note when every saved widget is known', () => {
    renderEditing();
    expect(within(bar()).queryByText(/newer version of the app/)).not.toBeInTheDocument();
  });
});

describe('saving', () => {
  it('shows Done as a spinner and disables Add widgets, Undo, Reset and Cancel while the save runs', () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockImplementation(() => new Promise(() => undefined));
    renderEditing({ layout });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    done().focus();
    act(() => {
      fireEvent.click(done());
    });
    // Done stays FOCUSABLE while it runs (aria-disabled, not disabled): a natively disabled
    // focused button would drop keyboard focus to <body> for the whole request.
    expect(done()).not.toBeDisabled();
    expect(done()).toHaveAttribute('aria-disabled', 'true');
    expect(done()).toHaveFocus();
    expect(done().querySelector('.animate-spin')).not.toBeNull();
    fireEvent.click(done());
    expect(layout.save).toHaveBeenCalledTimes(1);
    // And the pending state is spoken through the always-mounted status region.
    expect(within(bar()).getByText(copy.SAVING_NOTICE).closest('[aria-live="polite"]')).not.toBeNull();
    expect(inBar('Add widgets')).toBeDisabled();
    expect(inBar('Undo')).toHaveAttribute('aria-disabled', 'true');
    expect(inBar('Reset to default')).toBeDisabled();
    expect(inBar('Cancel')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove Due Today' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Due Today' })).toBeDisabled();
    // Escape never leaves edit mode (and cannot abandon a write in flight).
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(bar()).toBeInTheDocument();
  });

  it('shows a 409 inline, focuses Reload, keeps the alert up while reloading, then replaces the draft', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockRejectedValue(
      httpError(409, 'Your dashboard layout changed in another tab. Reload it before saving.')
    );
    const reloaded = layoutResponse({ format_version: 1, widgets: [{ id: 'kpi.due_today' }] }, { version: 7 });
    const pendingReload = deferred<DashboardLayoutResponse | null>();
    layout.reload.mockImplementation(() => pendingReload.promise);
    renderEditing({ layout });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    done().focus();
    await act(async () => {
      fireEvent.click(done());
    });
    const alert = within(bar()).getByRole('alert');
    expect(alert).toHaveTextContent('Your dashboard layout changed in another tab. Reload it before saving.');
    expect(reloadButton()).toHaveFocus();
    expect(done()).toBeDisabled();
    expect(inBar('Reset to default')).toBeDisabled();
    expect(inBar('Add widgets')).toBeDisabled();
    expect(inBar('Cancel')).toBeEnabled();

    fireEvent.click(reloadButton());
    expect(layout.reload).toHaveBeenCalledTimes(1);
    const inFlight = within(within(bar()).getByRole('alert')).getByRole('button', { name: /Reload saved layout/ });
    // Focusable while it runs (aria-disabled), so focus never drops; a second click is ignored.
    expect(inFlight).not.toBeDisabled();
    expect(inFlight).toHaveAttribute('aria-disabled', 'true');
    expect(inFlight).toHaveFocus();
    fireEvent.click(inFlight);
    expect(layout.reload).toHaveBeenCalledTimes(1);
    expect(within(bar()).getByText(copy.RELOADING_NOTICE)).toBeInTheDocument();
    expect(reloadButton().querySelector('.animate-spin')).not.toBeNull();

    await act(async () => {
      layout.set({ response: reloaded });
      pendingReload.resolve(reloaded);
    });
    expect(within(bar()).queryByRole('alert')).not.toBeInTheDocument();
    const notice = within(bar()).getByText(copy.RELOADED_NOTICE);
    expect(notice.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.getByRole('heading', { name: 'Customizing dashboard', level: 2 })).toHaveFocus();
    expect(kpiCells()).toEqual(['kpi.due_today']);
    expect(done()).toBeEnabled();
  });

  it('keeps the recovery action, focused and enabled, when the reload itself fails', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockRejectedValue(httpError(409, 'Changed in another tab.'));
    // useDashboardLayout.reload never rejects: a failed read resolves null.
    const pendingReload = deferred<DashboardLayoutResponse | null>();
    layout.reload.mockImplementation(() => pendingReload.promise);
    renderEditing({ layout });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    await act(async () => {
      fireEvent.click(done());
    });
    fireEvent.click(reloadButton());
    // Focus wanders while the read is out (a click on the page). The soft-locked button
    // would otherwise just keep it, so this is what shows the failure asks for it back.
    act(() => reloadButton().blur());
    expect(document.body).toHaveFocus();
    await act(async () => pendingReload.resolve(null));
    expect(within(bar()).getByRole('alert')).toHaveTextContent(copy.RELOAD_FAILED);
    expect(within(bar()).queryByText(copy.RELOADING_NOTICE)).not.toBeInTheDocument();
    expect(reloadButton()).toBeEnabled();
    expect(reloadButton()).not.toHaveAttribute('aria-disabled');
    expect(reloadButton()).toHaveFocus();
    // Still locked behind the reload: the failed read replaced nothing.
    expect(done()).toBeDisabled();
    expect(kpiCells()).toEqual(['kpi.due_today']);
  });

  it('treats a save answered for another company like a 409', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockRejectedValue(new DashboardLayoutCompanyChangedError());
    renderEditing({ layout });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    done().focus();
    await act(async () => {
      fireEvent.click(done());
    });
    expect(within(bar()).getByRole('alert')).toHaveTextContent(
      'Your active company changed. Reload the dashboard before saving its layout.'
    );
    expect(done()).toBeDisabled();
    expect(inBar('Reset to default')).toBeDisabled();
    expect(inBar('Add widgets')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove Due Today' })).toBeDisabled();
    expect(reloadButton()).toHaveFocus();
  });

  it('keeps the draft and returns focus to Done after a failure that is not a conflict; a retry succeeds', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.save.mockRejectedValueOnce(
      httpError(503, 'Your dashboard layout could not be saved because its audit record failed; nothing was changed.')
    );
    const { showToast } = renderEditing({ layout });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    await act(async () => {
      fireEvent.click(done());
    });
    expect(within(bar()).getByRole('alert')).toHaveTextContent(/audit record failed; nothing was changed/);
    expect(within(bar()).queryByRole('button', { name: /Reload saved layout/ })).not.toBeInTheDocument();
    expect(done()).toBeEnabled();
    expect(done()).toHaveFocus();
    expect(kpiCells()).toEqual(['kpi.due_today']);

    await act(async () => {
      fireEvent.click(done());
    });
    expect(layout.save).toHaveBeenCalledTimes(2);
    expect(layout.save.mock.calls[1][0]).toEqual(layout.save.mock.calls[0][0]);
    expect(showToast).toHaveBeenCalledWith('success', copy.SAVED_TOAST);
  });
});

describe('Reset', () => {
  it('resets through a confirmation and focuses Customize after the confirmation closes', async () => {
    const { layout, showToast } = renderEditing();
    fireEvent.click(inBar('Reset to default'));
    const confirm = screen.getByRole('dialog', { name: 'Reset dashboard to default?' });
    await settle();
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Reset to default' }));
    });
    await settle();
    expect(layout.reset).toHaveBeenCalledTimes(1);
    expect(layout.save).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('success', copy.RESET_TOAST);
    expect(screen.queryByRole('region', { name: 'Customizing dashboard' })).not.toBeInTheDocument();
    expect(customize()).toHaveFocus();
  });

  it('a 409 closes the confirmation, locks Reset and focuses Reload', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.reset.mockRejectedValue(httpError(409, 'Your dashboard layout changed in another tab.'));
    renderEditing({ layout });
    const reset = inBar('Reset to default');
    reset.focus();
    fireEvent.click(reset);
    const confirm = screen.getByRole('dialog', { name: 'Reset dashboard to default?' });
    await settle();
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Reset to default' }));
    });
    await settle();
    expect(screen.queryByRole('dialog', { name: 'Reset dashboard to default?' })).not.toBeInTheDocument();
    expect(inBar('Reset to default')).toBeDisabled();
    expect(reloadButton()).toHaveFocus();
    expect(document.activeElement).not.toBe(document.body);
  });

  it('any other failure closes the confirmation, shows the alert and focuses Reset', async () => {
    const layout = createFakeLayout({ saved: SAVED });
    layout.reset.mockRejectedValue(httpError(503, 'Reset failed on the server.'));
    renderEditing({ layout });
    fireEvent.click(inBar('Reset to default'));
    const confirm = screen.getByRole('dialog', { name: 'Reset dashboard to default?' });
    await settle();
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Reset to default' }));
    });
    await settle();
    expect(screen.queryByRole('dialog', { name: 'Reset dashboard to default?' })).not.toBeInTheDocument();
    expect(within(bar()).getByRole('alert')).toHaveTextContent('Reset failed on the server.');
    expect(inBar('Reset to default')).toHaveFocus();
  });

  it('offers no reset while the dashboard already shows the default', () => {
    renderEditing({ layout: createFakeLayout({ saved: null }) });
    const reset = inBar('Reset to default');
    expect(reset).toBeDisabled();
    expect(reset).toHaveAttribute('title', 'You are already using the default layout');
  });
});

describe('Undo', () => {
  it('is aria-disabled (never disabled) with nothing to undo, then restores a step and keeps focus', () => {
    renderEditing();
    const undo = inBar('Undo');
    expect(undo).toHaveAttribute('aria-disabled', 'true');
    expect(undo).toBeEnabled();
    undo.focus();
    fireEvent.click(undo);
    expect(announcer()).toHaveTextContent(copy.ENTER_ANNOUNCEMENT);

    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    expect(kpiCells()).toEqual(['kpi.due_today']);
    expect(inBar('Undo')).toHaveAttribute('aria-disabled', 'false');
    inBar('Undo').focus();
    fireEvent.click(inBar('Undo'));
    expect(kpiCells()).toEqual(['kpi.overdue', 'kpi.due_today']);
    expect(announcer()).toHaveTextContent('Undone: removed Overdue.');
    expect(inBar('Undo')).toHaveFocus();
    expect(inBar('Undo')).toHaveAttribute('aria-disabled', 'true');
  });
});

describe('focus after removing a widget', () => {
  it('goes to the next Remove badge in the zone, else the previous one, else Add widgets', () => {
    renderEditing();
    fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
    expect(screen.getByRole('button', { name: 'Remove Due Today' })).toHaveFocus();
    expect(announcer()).toHaveTextContent(copy.removedMessage('Overdue'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove Due Today' }));
    expect(inBar('Add widgets')).toHaveFocus();

    fireEvent.click(inBar('Undo'));
    fireEvent.click(inBar('Undo'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove Due Today' }));
    expect(screen.getByRole('button', { name: 'Remove Overdue' })).toHaveFocus();
  });
});

it('notes saved widgets the viewer cannot see right now', () => {
  const saved: DashboardLayoutValues = {
    format_version: 1,
    widgets: [{ id: 'kpi.overdue' }, { id: 'kpi.open_cars' }, { id: 'kpi.due_today' }],
  };
  renderEditing({ layout: createFakeLayout({ saved }), eligibility: SHIPPING });
  expect(within(bar()).getByText(copy.carriedNotice(1))).toBeInTheDocument();
  expect(copy.carriedNotice(1)).toMatch(/^1 widget in your saved layout is not available to you right now/);
  expect(copy.carriedNotice(2)).toMatch(/^2 widgets in your saved layout are not available/);
});

it('uses AA-contrast text for its small labels (no slate-500 text on the panel)', () => {
  renderEditing();
  fireEvent.click(screen.getByRole('button', { name: 'Remove Overdue' }));
  expect(bar().querySelectorAll('[class*="text-slate-500"]')).toHaveLength(0);
});
