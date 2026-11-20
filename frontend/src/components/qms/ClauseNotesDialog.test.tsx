/**
 * ClauseNotesDialog — editing a QMS clause's compliance notes.
 *
 * Contract under test:
 *  - The text is seeded from the stored notes every time the dialog opens.
 *  - Save is disabled until the text differs from what is stored, and while it is over the
 *    server's 5,000-character cap (QMSClauseUpdate.compliance_notes max_length=5000).
 *  - Save sends the typed text; an all-whitespace text is sent as null (clears the notes).
 *  - Non-optimistic: the dialog asks to close only after `onSave` resolves. A refusal shows the
 *    server's `detail` inside the dialog (role=alert) and keeps the typed text.
 *  - Cancel / X / Escape go through the unsaved-changes guard (window.confirm only when dirty).
 *    A backdrop click never closes. While a save is in flight nothing closes it.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ClauseNotesDialog, { CLAUSE_NOTES_MAX_LENGTH } from './ClauseNotesDialog';

type SaveFn = (notes: string | null) => Promise<void>;

const CLAUSE_NUMBER = '8.5.1';
const CLAUSE_TITLE = 'Control of production and service provision';
const STORED = 'Router travels with the job.\nFirst-article on every new revision.';

interface RenderOptions {
  open?: boolean;
  initialNotes?: string | null;
  onSave?: jest.Mock<Promise<void>, [string | null]>;
  onClose?: jest.Mock<void, []>;
}

function renderDialog({
  open = true,
  initialNotes = STORED,
  onSave = jest.fn<Promise<void>, [string | null]>().mockResolvedValue(undefined),
  onClose = jest.fn<void, []>(),
}: RenderOptions = {}) {
  const props = { clauseNumber: CLAUSE_NUMBER, clauseTitle: CLAUSE_TITLE, onSave: onSave as SaveFn, onClose };
  const utils = render(<ClauseNotesDialog open={open} initialNotes={initialNotes} {...props} />);
  const rerenderWith = (next: { open: boolean; initialNotes?: string | null }) =>
    utils.rerender(<ClauseNotesDialog open={next.open} initialNotes={next.initialNotes ?? initialNotes} {...props} />);
  return { ...utils, onSave, onClose, rerenderWith };
}

function deferred() {
  let resolve!: () => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const dialog = () => screen.getByRole('dialog', { name: `Notes for clause ${CLAUSE_NUMBER}` });
const notesBox = () => screen.getByLabelText('Compliance notes') as HTMLTextAreaElement;
const saveButton = () => screen.getByRole('button', { name: 'Save notes' });
const cancelButton = () => screen.getByRole('button', { name: 'Cancel' });
const closeButton = () => screen.getByRole('button', { name: 'Close' });
const type = (value: string) => fireEvent.change(notesBox(), { target: { value } });
const pressEscape = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });

const CLOSE_TRIGGERS: Array<[string, () => void]> = [
  ['Cancel', () => fireEvent.click(cancelButton())],
  ['the X button', () => fireEvent.click(closeButton())],
  ['Escape', pressEscape],
];

let confirmSpy: jest.SpyInstance<boolean, [message?: string]>;

beforeEach(() => {
  confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  confirmSpy.mockRestore();
});

describe('the client cap', () => {
  it('is 5,000 characters (kept in step by hand with QMSClauseUpdate.compliance_notes max_length)', () => {
    expect(CLAUSE_NOTES_MAX_LENGTH).toBe(5000);
  });
});

describe('a reload while the dialog is open', () => {
  it('never overwrites the typed text when newer stored notes arrive', () => {
    const { rerenderWith } = renderDialog();
    type('Typing after a reopen');
    // The page's reload after an earlier save lands while the dialog is still open.
    rerenderWith({ open: true, initialNotes: 'Notes the server now holds' });
    expect(notesBox().value).toBe('Typing after a reopen');
  });
});

describe('opening', () => {
  it('renders nothing while closed', () => {
    renderDialog({ open: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('names the dialog and heading after the clause and seeds the stored notes, line breaks kept', () => {
    renderDialog();
    expect(dialog()).toBeInTheDocument();
    expect(
      within(dialog()).getByRole('heading', { name: `Notes for ${CLAUSE_NUMBER} ${CLAUSE_TITLE}` })
    ).toBeInTheDocument();
    expect(notesBox().value).toBe(STORED);
  });

  it('starts empty when the clause has no notes', () => {
    renderDialog({ initialNotes: null });
    expect(notesBox().value).toBe('');
  });

  it('re-seeds from the stored notes on every open, discarding text left from a previous open', () => {
    const { rerenderWith } = renderDialog({ open: false });
    rerenderWith({ open: true });
    expect(notesBox().value).toBe(STORED);

    type('typed, then abandoned');
    rerenderWith({ open: false });
    rerenderWith({ open: true });
    expect(notesBox().value).toBe(STORED);

    // Notes changed on the server between opens: the newest stored value wins.
    rerenderWith({ open: false, initialNotes: 'Reloaded from the server' });
    rerenderWith({ open: true, initialNotes: 'Reloaded from the server' });
    expect(notesBox().value).toBe('Reloaded from the server');
  });
});

describe('Save enablement', () => {
  it('is disabled until the text differs from what is stored, and again once it matches', () => {
    const { onSave } = renderDialog();
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());
    expect(onSave).not.toHaveBeenCalled();

    type(`${STORED} More.`);
    expect(saveButton()).toBeEnabled();

    type(STORED);
    expect(saveButton()).toBeDisabled();
  });
});

describe('character counter and the 5,000-character cap', () => {
  const max = CLAUSE_NOTES_MAX_LENGTH.toLocaleString();

  it('describes the textarea with a live count of the cap', () => {
    renderDialog({ initialNotes: null });
    expect(notesBox()).toHaveAccessibleDescription(new RegExp(`^0 / ${max} characters\\.`));

    type('Calibrated gauges only.');
    expect(notesBox()).toHaveAccessibleDescription(new RegExp(`^23 / ${max} characters\\.`));
    expect(screen.getByText(/does not change the clause's compliance status/)).toBeInTheDocument();
  });

  it('accepts exactly the cap', () => {
    renderDialog({ initialNotes: null });
    type('a'.repeat(CLAUSE_NOTES_MAX_LENGTH));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(notesBox()).not.toHaveAttribute('aria-invalid');
    expect(saveButton()).toBeEnabled();
  });

  it('flags one character over the cap inline and refuses to save', () => {
    const { onSave } = renderDialog({ initialNotes: null });
    type('a'.repeat(CLAUSE_NOTES_MAX_LENGTH + 1));

    const alert = within(dialog()).getByRole('alert');
    expect(alert).toHaveTextContent(`Notes can be at most ${max} characters.`);
    expect(notesBox()).toHaveAttribute('aria-invalid', 'true');
    expect(notesBox()).toHaveAccessibleDescription(
      expect.stringContaining(`${(CLAUSE_NOTES_MAX_LENGTH + 1).toLocaleString()} / ${max} characters.`)
    );
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());
    expect(onSave).not.toHaveBeenCalled();

    // Trimming back under the cap clears the error.
    type('a'.repeat(CLAUSE_NOTES_MAX_LENGTH));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
  });
});

describe('what Save sends', () => {
  it('sends the typed text exactly, line breaks included', async () => {
    const { onSave } = renderDialog();
    const typed = 'Router travels with the job.\n\nFAI required on revision change.';
    type(typed);
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(typed);
  });

  it('sends text with leading and trailing whitespace exactly as typed (never trimmed)', async () => {
    const { onSave } = renderDialog();
    const typed = '  indented first line\n';
    type(typed);
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(typed);
  });

  it('sends null for an all-whitespace text, which clears the stored notes', async () => {
    const { onSave } = renderDialog();
    type('   \n\t  \n');
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0]).toBeNull();
  });

  it('sends null when the stored notes are deleted outright', async () => {
    const { onSave } = renderDialog();
    type('');
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave.mock.calls[0][0]).toBeNull();
  });
});

describe('a save the server accepts', () => {
  it('closes without asking to discard', async () => {
    const { onClose } = renderDialog();
    type('Accepted text');
    fireEvent.click(saveButton());

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(confirmSpy).not.toHaveBeenCalled();
    await screen.findByRole('button', { name: 'Save notes' });
  });

  it('marks the edit saved, so the page-unload guard stops holding it', async () => {
    // onClose is a no-op here, so the dialog stays mounted with its (now saved) text: only
    // markSaved() can explain the unload guard letting go.
    const { onClose } = renderDialog();
    type('Accepted text');

    const unload = () => window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
    expect(unload()).toBe(false); // dirty and unsaved: the guard cancels the unload

    fireEvent.click(saveButton());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    await screen.findByRole('button', { name: 'Save notes' });
    expect(notesBox().value).toBe('Accepted text'); // still dirty against the stale initialNotes
    expect(unload()).toBe(true);
  });
});

describe('a save the server refuses', () => {
  it("shows the server's detail inside the dialog, keeps the text, and stays open", async () => {
    const detail = 'Only Admin, Manager or Quality can edit clause notes.';
    const onSave = jest.fn<Promise<void>, [string | null]>().mockRejectedValue({ response: { data: { detail } } });
    const { onClose } = renderDialog({ onSave });
    type('Text the server refuses');
    fireEvent.click(saveButton());

    const alert = await within(dialog()).findByRole('alert');
    expect(alert).toHaveTextContent(detail);
    expect(notesBox().value).toBe('Text the server refuses');
    expect(notesBox()).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
    // Ready to retry.
    expect(saveButton()).toBeEnabled();
    expect(saveButton()).toHaveTextContent('Save notes');
  });

  it('shows a network error by its own message (the shared apiErrorDetail rule)', async () => {
    const onSave = jest.fn<Promise<void>, [string | null]>().mockRejectedValue(new Error('Network Error'));
    renderDialog({ onSave });
    type('Text');
    fireEvent.click(saveButton());

    expect(await within(dialog()).findByRole('alert')).toHaveTextContent('Network Error');
  });

  it.each<[string, unknown]>([
    ['an empty detail', { response: { data: { detail: '' } } }],
    ['a whitespace-only detail', { response: { data: { detail: '   ' } } }],
    ['a structured (non-string) detail', { response: { data: { detail: { code: 'conflict' } } } }],
  ])('falls back to a generic message for %s', async (_label, rejection) => {
    const onSave = jest.fn<Promise<void>, [string | null]>().mockRejectedValue(rejection);
    renderDialog({ onSave });
    type('Text');
    fireEvent.click(saveButton());

    const alert = await within(dialog()).findByRole('alert');
    expect(alert).toHaveTextContent('The notes could not be saved. Try again.');
    expect(alert).not.toHaveTextContent('[object Object]');
  });

  it('clears the error when the next save succeeds and when the dialog is reopened', async () => {
    const onSave = jest
      .fn<Promise<void>, [string | null]>()
      .mockRejectedValueOnce({ response: { data: { detail: 'Refused once' } } })
      .mockResolvedValueOnce(undefined);
    const { onClose, rerenderWith } = renderDialog({ onSave });
    type('Retry me');
    fireEvent.click(saveButton());
    expect(await within(dialog()).findByText('Refused once')).toBeInTheDocument();

    fireEvent.click(saveButton());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Refused once')).not.toBeInTheDocument();

    // A refusal, then the parent closes and reopens: the stale reason must not reappear.
    onSave.mockRejectedValueOnce({ response: { data: { detail: 'Refused again' } } });
    type('Another try');
    fireEvent.click(saveButton());
    expect(await within(dialog()).findByText('Refused again')).toBeInTheDocument();
    rerenderWith({ open: false });
    rerenderWith({ open: true });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('closing', () => {
  it.each(CLOSE_TRIGGERS)('%s closes a clean dialog without asking', (_label, trigger) => {
    const { onClose } = renderDialog();
    trigger();
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it.each(CLOSE_TRIGGERS)(
    '%s on unsaved text asks first; declining keeps the dialog and the text',
    (_label, trigger) => {
      confirmSpy.mockReturnValue(false);
      const { onClose } = renderDialog();
      type('Unsaved thought');
      trigger();

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(onClose).not.toHaveBeenCalled();
      expect(dialog()).toBeInTheDocument();
      expect(notesBox().value).toBe('Unsaved thought');
    }
  );

  it.each(CLOSE_TRIGGERS)('%s on unsaved text asks first; confirming closes', (_label, trigger) => {
    confirmSpy.mockReturnValue(true);
    const { onClose } = renderDialog();
    type('Unsaved thought');
    trigger();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('leaves no page-unload guard behind once a discarded dialog is closed', () => {
    confirmSpy.mockReturnValue(true);
    const { rerenderWith } = renderDialog();
    type('Unsaved thought');
    fireEvent.click(cancelButton());
    rerenderWith({ open: false }); // the parent closes it

    const unload = () => window.dispatchEvent(new Event('beforeunload', { cancelable: true }));
    expect(unload()).toBe(true);
  });

  it.each([
    ['clean', null],
    ['with unsaved text', 'Unsaved thought'],
  ])('a backdrop click never closes it (%s)', (_label, typed) => {
    const { onClose } = renderDialog();
    if (typed !== null) type(typed);
    const backdrop = dialog().parentElement as HTMLElement;
    expect(backdrop).toHaveAttribute('role', 'presentation');

    fireEvent.click(backdrop);

    expect(onClose).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(dialog()).toBeInTheDocument();
  });
});

describe('while a save is in flight', () => {
  it('locks the dialog: Cancel and X disabled, Escape ignored, no second save', async () => {
    const pending = deferred();
    const onSave = jest.fn<Promise<void>, [string | null]>().mockReturnValue(pending.promise);
    const { onClose } = renderDialog({ onSave });
    type('Slow save');
    fireEvent.click(saveButton());

    const saving = await screen.findByRole('button', { name: /Saving\.\.\./ });
    expect(saving).toBeDisabled();
    expect(cancelButton()).toBeDisabled();
    expect(closeButton()).toBeDisabled();
    expect(notesBox()).toBeDisabled();

    fireEvent.click(cancelButton());
    fireEvent.click(closeButton());
    pressEscape();
    fireEvent.click(dialog().parentElement as HTMLElement);
    fireEvent.click(saving);

    expect(onClose).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(onSave).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('unlocks again after a refusal', async () => {
    const pending = deferred();
    const onSave = jest.fn<Promise<void>, [string | null]>().mockReturnValue(pending.promise);
    renderDialog({ onSave });
    type('Slow refusal');
    fireEvent.click(saveButton());
    await screen.findByRole('button', { name: /Saving\.\.\./ });

    await act(async () => {
      pending.reject({ response: { data: { detail: 'Refused' } } });
      await pending.promise.catch(() => undefined);
    });

    expect(cancelButton()).toBeEnabled();
    expect(closeButton()).toBeEnabled();
    expect(notesBox()).toBeEnabled();
    expect(saveButton()).toBeEnabled();
  });
});
