/**
 * Purchasing → Quality flow-down: the profile form and the clause catalog (AS9100D 8.4.3).
 *
 * Every write here is server-gated, so the screen shows only what the server returned:
 *  - the profile is a version compare-and-swap. A save sends the version it read; a 409 shows the
 *    server's text and re-reads the profile, so the next save starts from what is stored.
 *  - REGRESSION: after a successful save, a further edit must re-enable Save. The form used to be
 *    reset from inside the submit handler, which left react-hook-form's dirty tracking dead, so
 *    Save stayed disabled after the next keystroke and a second edit could not be saved.
 *  - the clause dialog adds a clause with its code, edits one with the code read-only and sends
 *    only the fields that changed, and Retire / Reactivate send the one flag.
 *
 * The api module is mocked at its boundary. Clause codes and wording are invented.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import { QualityFlowdownSettings } from './QualityFlowdownSettings';
import type { POFlowdownProfile, POQualityClause } from '../../types/poFlowdown';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPOFlowdownProfile: jest.fn(),
    updatePOFlowdownProfile: jest.fn(),
    getPOQualityClauses: jest.fn(),
    createPOQualityClause: jest.fn(),
    updatePOQualityClause: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const PROFILE: POFlowdownProfile = {
  id: 3,
  enabled: true,
  heading: 'Supplier Quality Terms (test)',
  document_reference: 'SQR-TEST Rev B',
  body: 'First test paragraph.',
  version: 2,
  updated_at: '2026-11-19T15:00:00Z',
  updated_by: 1,
};

const CLAUSES: POQualityClause[] = [
  {
    id: 11,
    code: 'QX1',
    title: 'Alpha requirement',
    body: 'Alpha clause text.',
    default_selected: true,
    is_active: true,
    sort_order: 20,
  },
  {
    id: 12,
    code: 'QX2',
    title: 'Bravo requirement',
    body: null,
    default_selected: false,
    is_active: true,
    sort_order: 10,
  },
  {
    id: 19,
    code: 'QX9',
    title: 'Retired requirement',
    body: null,
    default_selected: false,
    is_active: false,
    sort_order: 0,
  },
];

function apiError(status: number, detail: string) {
  return Object.assign(new Error(detail), { response: { status, data: { detail } } });
}

function renderSettings() {
  return render(
    <ToastProvider>
      <QualityFlowdownSettings />
    </ToastProvider>
  );
}

const headingInput = () => screen.getByLabelText(/^Heading/) as HTMLInputElement;
const saveButton = () => screen.getByRole('button', { name: /save profile|saving/i });
const catalogTable = () => screen.getByTestId('data-table');

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getPOFlowdownProfile.mockResolvedValue({ ...PROFILE });
  mockedApi.getPOQualityClauses.mockResolvedValue(CLAUSES.map(clause => ({ ...clause })));
});

describe('QualityFlowdownSettings — profile', () => {
  test('a save sends the version it read, and after it a further edit re-enables Save (regression)', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOFlowdownProfile
      .mockResolvedValueOnce({ ...PROFILE, heading: 'Revised terms (test)', version: 3 })
      .mockResolvedValueOnce({ ...PROFILE, heading: 'Revised terms (test) again', version: 4 });
    renderSettings();

    await screen.findByDisplayValue('Supplier Quality Terms (test)');
    expect(saveButton()).toBeDisabled(); // a saved profile with nothing changed

    await user.clear(headingInput());
    await user.type(headingInput(), 'Revised terms (test)');
    expect(saveButton()).toBeEnabled();
    await user.click(saveButton());

    await waitFor(() => expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledTimes(1));
    expect(mockedApi.updatePOFlowdownProfile).toHaveBeenLastCalledWith({
      enabled: true,
      heading: 'Revised terms (test)',
      document_reference: 'SQR-TEST Rev B',
      body: 'First test paragraph.',
      version: 2,
    });
    expect(await screen.findByText(/Flow-down profile saved/)).toBeInTheDocument();
    await waitFor(() => expect(saveButton()).toBeDisabled()); // clean again, at the saved value
    expect(headingInput()).toHaveValue('Revised terms (test)');

    // THE BUG: this edit used to leave Save disabled.
    await user.type(headingInput(), ' again');
    expect(saveButton()).toBeEnabled();
    await user.click(saveButton());

    await waitFor(() => expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledTimes(2));
    expect(mockedApi.updatePOFlowdownProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ heading: 'Revised terms (test) again', version: 3 })
    );
  });

  test('toggling the enabled checkbox alone is a change Save sends', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOFlowdownProfile.mockResolvedValue({ ...PROFILE, enabled: false, version: 3 });
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Terms (test)');

    await user.click(screen.getByRole('checkbox', { name: /print the flow-down block/i }));
    await user.click(saveButton());

    await waitFor(() =>
      expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledWith(
        expect.objectContaining({ enabled: false, version: 2 })
      )
    );
  });

  test('a 409 shows the server text, reloads the stored profile, and the next save uses its version', async () => {
    const user = userEvent.setup();
    const stale = 'The quality flow-down settings were changed by someone else (test).';
    mockedApi.updatePOFlowdownProfile
      .mockRejectedValueOnce(apiError(409, stale))
      .mockResolvedValueOnce({ ...PROFILE, heading: 'Mine after reload', version: 6 });
    mockedApi.getPOFlowdownProfile
      .mockResolvedValueOnce({ ...PROFILE })
      .mockResolvedValueOnce({ ...PROFILE, heading: 'Someone else saved this', version: 5 });
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Terms (test)');

    await user.clear(headingInput());
    await user.type(headingInput(), 'My lost edit');
    await user.click(saveButton());

    expect(await screen.findByText(/The profile was reloaded with the saved version/)).toHaveTextContent(stale);
    await waitFor(() => expect(headingInput()).toHaveValue('Someone else saved this'));
    expect(mockedApi.getPOFlowdownProfile).toHaveBeenCalledTimes(2);
    expect(saveButton()).toBeDisabled();

    await user.clear(headingInput());
    await user.type(headingInput(), 'Mine after reload');
    expect(saveButton()).toBeEnabled();
    await user.click(saveButton());

    await waitFor(() => expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledTimes(2));
    expect(mockedApi.updatePOFlowdownProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ heading: 'Mine after reload', version: 5 })
    );
  });

  test('any other refusal keeps the edit on screen and toasts the server text, without a reload', async () => {
    const user = userEvent.setup();
    const lost = 'The audit record for the quality flow-down settings could not be saved (test).';
    mockedApi.updatePOFlowdownProfile.mockRejectedValueOnce(apiError(503, lost));
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Terms (test)');

    await user.clear(headingInput());
    await user.type(headingInput(), 'Unsaved edit');
    await user.click(saveButton());

    expect(await screen.findByText(lost)).toBeInTheDocument();
    expect(headingInput()).toHaveValue('Unsaved edit');
    expect(saveButton()).toBeEnabled();
    expect(mockedApi.getPOFlowdownProfile).toHaveBeenCalledTimes(1);
  });

  test('a never-saved profile can be saved as shown, blanks going out as null', async () => {
    const user = userEvent.setup();
    mockedApi.getPOFlowdownProfile.mockResolvedValue({
      id: null,
      enabled: false,
      heading: 'Supplier Quality Requirements',
      document_reference: null,
      body: null,
      version: 0,
    });
    mockedApi.updatePOFlowdownProfile.mockResolvedValue({ ...PROFILE, enabled: false, version: 1 });
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Requirements');

    expect(screen.getByText(/Not saved yet/)).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
    await user.click(saveButton());

    await waitFor(() =>
      expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledWith({
        enabled: false,
        heading: 'Supplier Quality Requirements',
        document_reference: null,
        body: null,
        version: 0,
      })
    );
  });

  test('the first save of the default profile (several fields, a trimmed one) leaves the form editable (regression)', async () => {
    const user = userEvent.setup();
    mockedApi.getPOFlowdownProfile.mockResolvedValue({
      id: null,
      enabled: false,
      heading: 'Supplier Quality Requirements',
      document_reference: null,
      body: null,
      version: 0,
    });
    mockedApi.updatePOFlowdownProfile
      .mockResolvedValueOnce({ ...PROFILE, id: 4, version: 1 })
      .mockResolvedValueOnce({ ...PROFILE, id: 4, heading: 'Second edit (test)', version: 2 });
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Requirements');

    await user.click(screen.getByRole('checkbox', { name: /print the flow-down block/i }));
    await user.clear(headingInput());
    await user.type(headingInput(), PROFILE.heading);
    await user.type(screen.getByLabelText(/^Document reference/), ` ${PROFILE.document_reference} `);
    await user.type(screen.getByLabelText(/^Standard flow-down text/), PROFILE.body as string);
    await user.click(saveButton());

    await waitFor(() => expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledTimes(1));
    expect(mockedApi.updatePOFlowdownProfile).toHaveBeenLastCalledWith({
      enabled: true,
      heading: PROFILE.heading,
      document_reference: PROFILE.document_reference, // trimmed
      body: PROFILE.body,
      version: 0,
    });
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(screen.queryByText(/Not saved yet/)).toBeNull();
    expect(screen.getByLabelText(/^Document reference/)).toHaveValue(PROFILE.document_reference);

    await user.clear(headingInput());
    await user.type(headingInput(), 'Second edit (test)');
    expect(saveButton()).toBeEnabled();
    await user.click(saveButton());

    await waitFor(() => expect(mockedApi.updatePOFlowdownProfile).toHaveBeenCalledTimes(2));
    expect(mockedApi.updatePOFlowdownProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ heading: 'Second edit (test)', version: 1 })
    );
  });

  test('a blank heading is refused before any request', async () => {
    const user = userEvent.setup();
    renderSettings();
    await screen.findByDisplayValue('Supplier Quality Terms (test)');

    await user.clear(headingInput());
    await user.click(saveButton());

    expect(await screen.findByText('A heading is required.')).toBeInTheDocument();
    expect(mockedApi.updatePOFlowdownProfile).not.toHaveBeenCalled();
  });

  test('a failed load offers a retry that re-reads it', async () => {
    const user = userEvent.setup();
    mockedApi.getPOFlowdownProfile.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ...PROFILE });
    jest.spyOn(console, 'error').mockImplementation(() => undefined);
    renderSettings();

    expect(await screen.findByText('Could not load the flow-down profile.')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: /retry/i })[0]);

    expect(await screen.findByDisplayValue('Supplier Quality Terms (test)')).toBeInTheDocument();
  });
});

describe('QualityFlowdownSettings — clause catalog', () => {
  test('lists every clause, retired ones included, in print order', async () => {
    renderSettings();

    await within(await screen.findByTestId('data-table')).findByText('QX1');
    expect(mockedApi.getPOQualityClauses).toHaveBeenCalledWith({ include_inactive: true });
    const codes = within(catalogTable())
      .getAllByRole('row')
      .slice(1)
      .map(row => row.querySelector('td')?.textContent);
    expect(codes).toEqual(['QX9', 'QX2', 'QX1']);
    expect(within(catalogTable()).getByRole('button', { name: 'Reactivate clause QX9' })).toBeInTheDocument();
    expect(within(catalogTable()).getByRole('button', { name: 'Retire clause QX1' })).toBeInTheDocument();
  });

  test('Add clause creates it with its code and the next sort position, then lists it', async () => {
    const user = userEvent.setup();
    mockedApi.createPOQualityClause.mockResolvedValue({
      id: 30,
      code: 'QX5',
      title: 'Echo requirement',
      body: 'Echo clause text.',
      default_selected: true,
      is_active: true,
      sort_order: 30,
    });
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(screen.getByRole('button', { name: 'Add clause' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Add quality clause' })).toBeInTheDocument();
    const code = within(dialog).getByLabelText(/^Code/);
    expect(code).not.toHaveAttribute('readonly');
    expect(within(dialog).getByLabelText(/^Sort order/)).toHaveValue(30); // after the last (20) + 10
    await user.type(code, 'QX5');
    await user.type(within(dialog).getByLabelText(/^Title/), 'Echo requirement');
    await user.type(within(dialog).getByLabelText(/^Clause text/), 'Echo clause text.');
    await user.click(within(dialog).getByRole('checkbox', { name: /pre-check on new purchase orders/i }));
    await user.click(within(dialog).getByRole('button', { name: 'Add clause' }));

    await waitFor(() =>
      expect(mockedApi.createPOQualityClause).toHaveBeenCalledWith({
        code: 'QX5',
        title: 'Echo requirement',
        body: 'Echo clause text.',
        default_selected: true,
        is_active: true,
        sort_order: 30,
      })
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText('Clause QX5 saved.')).toBeInTheDocument();
    expect(within(catalogTable()).getByText('QX5')).toBeInTheDocument();
  });

  test('a refused add (duplicate code) keeps the dialog open with the server text', async () => {
    const user = userEvent.setup();
    const duplicate = "A quality clause with code 'qx1' already exists (test).";
    mockedApi.createPOQualityClause.mockRejectedValue(apiError(409, duplicate));
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(screen.getByRole('button', { name: 'Add clause' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/^Code/), 'qx1');
    await user.type(within(dialog).getByLabelText(/^Title/), 'Duplicate');
    await user.click(within(dialog).getByRole('button', { name: 'Add clause' }));

    expect(await screen.findByText(duplicate)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(catalogTable()).queryByText('qx1')).toBeNull();
  });

  test('Edit shows the code read-only and sends only the fields that changed', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOQualityClause.mockResolvedValue({ ...CLAUSES[0], title: 'Alpha requirement v2' });
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(within(catalogTable()).getByRole('button', { name: 'Edit clause QX1' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Edit clause QX1' })).toBeInTheDocument();
    const code = within(dialog).getByLabelText(/^Code/);
    expect(code).toHaveValue('QX1');
    expect(code).toHaveAttribute('readonly');
    expect(within(dialog).getByText('The code cannot change after the clause is created.')).toBeInTheDocument();

    const title = within(dialog).getByLabelText(/^Title/);
    await user.clear(title);
    await user.type(title, 'Alpha requirement v2');
    await user.click(within(dialog).getByRole('button', { name: 'Save clause' }));

    await waitFor(() =>
      expect(mockedApi.updatePOQualityClause).toHaveBeenCalledWith(11, { title: 'Alpha requirement v2' })
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(within(catalogTable()).getByText('Alpha requirement v2')).toBeInTheDocument();
  });

  test('an edit that changes nothing sends no request', async () => {
    const user = userEvent.setup();
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(within(catalogTable()).getByRole('button', { name: 'Edit clause QX2' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Save clause' }));

    expect(await screen.findByText('Nothing changed on clause QX2.')).toBeInTheDocument();
    expect(mockedApi.updatePOQualityClause).not.toHaveBeenCalled();
  });

  test('Retire sends only is_active false, and the row then offers Reactivate', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOQualityClause.mockResolvedValue({ ...CLAUSES[0], is_active: false });
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(within(catalogTable()).getByRole('button', { name: 'Retire clause QX1' }));

    await waitFor(() => expect(mockedApi.updatePOQualityClause).toHaveBeenCalledWith(11, { is_active: false }));
    expect(await screen.findByText(/Clause QX1 retired/)).toBeInTheDocument();
    expect(within(catalogTable()).getByRole('button', { name: 'Reactivate clause QX1' })).toBeInTheDocument();
    expect(mockedApi.createPOQualityClause).not.toHaveBeenCalled();
  });

  test('a refused retire leaves the row as it was', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOQualityClause.mockRejectedValue(apiError(503, 'Audit unavailable (test).'));
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX1');

    await user.click(within(catalogTable()).getByRole('button', { name: 'Retire clause QX1' }));

    expect(await screen.findByText('Audit unavailable (test).')).toBeInTheDocument();
    expect(within(catalogTable()).getByRole('button', { name: 'Retire clause QX1' })).toBeEnabled();
  });

  test('Reactivate sends only is_active true', async () => {
    const user = userEvent.setup();
    mockedApi.updatePOQualityClause.mockResolvedValue({ ...CLAUSES[2], is_active: true });
    renderSettings();
    await within(await screen.findByTestId('data-table')).findByText('QX9');

    await user.click(within(catalogTable()).getByRole('button', { name: 'Reactivate clause QX9' }));

    await waitFor(() => expect(mockedApi.updatePOQualityClause).toHaveBeenCalledWith(19, { is_active: true }));
    expect(await screen.findByText(/Clause QX9 reactivated/)).toBeInTheDocument();
    expect(within(catalogTable()).getByRole('button', { name: 'Retire clause QX9' })).toBeInTheDocument();
  });
});
