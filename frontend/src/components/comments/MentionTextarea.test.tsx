/**
 * MentionTextarea — the comment field with structured @mentions.
 *
 * What is pinned here, in the order it matters:
 *
 *  - TRIGGER: `@` + letters (at the start or after whitespace) searches
 *    `GET /users/mention-search` with the text after the `@`, debounced; an
 *    email address never opens the list.
 *  - ARIA 1.2 COMBOBOX wiring on the textarea: `aria-expanded` /
 *    `aria-controls` (only while open) / `aria-activedescendant`, a listbox of
 *    `role="option"` rows with `aria-selected`, and a live region announcing how
 *    many people were found.
 *  - KEYBOARD: Up/Down move, Enter or Tab inserts, and the pick writes
 *    `@Display Name ` into the text AND adds the id as a chip.
 *  - ESCAPE closes the list ONLY: inside a `<Modal>` the dialog stays open.
 *  - CHIPS ARE THE TRUTH: removing a chip leaves the text; deleting the text
 *    leaves the chip; the 20-mention cap is enforced client-side.
 */

import React, { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import MentionTextarea, { findMentionQuery } from './MentionTextarea';
import type { CommentPerson } from '../../types/comments';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    searchMentionableUsers: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const JAMIE: CommentPerson = { id: 5, display_name: 'Jamie Okafor' };
const JANA: CommentPerson = { id: 6, display_name: 'Jana Whitfield' };

interface HarnessProps {
  initialValue?: string;
  initialMentions?: CommentPerson[];
  maxMentions?: number;
  onSubmitShortcut?: () => void;
  onMentions?: (mentions: CommentPerson[]) => void;
  /** Render a button that clears the field from OUTSIDE, as a successful post does. */
  withReset?: boolean;
}

/** Controlled wrapper — the component is controlled, like its callers. */
function Harness({
  initialValue = '',
  initialMentions = [],
  maxMentions,
  onSubmitShortcut,
  onMentions,
  withReset = false,
}: HarnessProps) {
  const [value, setValue] = useState(initialValue);
  const [mentions, setMentions] = useState<CommentPerson[]>(initialMentions);
  return (
    <>
      <MentionTextarea
        label="Add a comment"
        value={value}
        onChange={setValue}
        mentions={mentions}
        onMentionsChange={next => {
          setMentions(next);
          onMentions?.(next);
        }}
        maxMentions={maxMentions}
        onSubmitShortcut={onSubmitShortcut}
      />
      {withReset && (
        <button
          type="button"
          onClick={() => {
            setValue('');
            setMentions([]);
          }}
        >
          Reset field
        </button>
      )}
    </>
  );
}

const field = () => screen.getByRole('combobox', { name: /add a comment/i }) as HTMLTextAreaElement;

const listboxOf = (input: HTMLElement) => {
  const id = input.getAttribute('aria-controls');
  if (!id) throw new Error('combobox is closed (no aria-controls)');
  const list = document.getElementById(id);
  if (!list) throw new Error(`listbox #${id} missing`);
  return list;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.searchMentionableUsers.mockResolvedValue([JAMIE, JANA]);
});

describe('findMentionQuery', () => {
  it('finds a trigger at the start or after whitespace, up to the caret', () => {
    expect(findMentionQuery('@ja', 3)).toEqual({ start: 0, query: 'ja' });
    expect(findMentionQuery('hi @Jamie Ok', 12)).toEqual({ start: 3, query: 'Jamie Ok' });
    expect(findMentionQuery('see (@ana', 9)).toEqual({ start: 5, query: 'ana' });
  });

  it('never triggers on an email address, a bare @, or a second space', () => {
    expect(findMentionQuery('mail a@example.com', 18)).toBeNull();
    expect(findMentionQuery('@', 1)).toBeNull();
    expect(findMentionQuery('@1abc', 5)).toBeNull();
    expect(findMentionQuery('@Jamie Okafor said', 18)).toBeNull();
  });

  it('only reads the text before the caret', () => {
    expect(findMentionQuery('@jamie and more', 6)).toEqual({ start: 0, query: 'jamie' });
  });
});

describe('MentionTextarea', () => {
  it('searches the text after @ (debounced) and opens an ARIA 1.2 listbox', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = field();
    expect(input).toHaveAttribute('aria-autocomplete', 'list');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).not.toHaveAttribute('aria-controls');

    await user.type(input, 'hi @ja');

    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    // Debounced: one search for the settled query, not one per keystroke.
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledTimes(1);
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('ja', 8);

    const list = listboxOf(input);
    expect(list).toHaveAttribute('role', 'listbox');
    const options = within(list).getAllByRole('option');
    expect(options.map(o => o.textContent)).toEqual(['Jamie Okafor', 'Jana Whitfield']);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');
    expect(options[1]).toHaveAttribute('aria-selected', 'false');
    expect(input).toHaveAttribute('aria-activedescendant', options[0].id);
    // Portaled out of the field's DOM, so a scroll container cannot clip it.
    expect(input.parentElement?.contains(list)).toBe(false);
    expect(screen.getByRole('status')).toHaveTextContent('2 people found');
  });

  it('does not open for an email address', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(field(), 'write to a@example');
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 300));
    });
    expect(mockedApi.searchMentionableUsers).not.toHaveBeenCalled();
    expect(field()).toHaveAttribute('aria-expanded', 'false');
  });

  /** Focus the field with the caret at the end, as an editor opening does. */
  const focusAtEnd = (input: HTMLTextAreaElement) =>
    act(() => {
      input.setSelectionRange(input.value.length, input.value.length);
      input.focus();
    });
  const pastDebounce = () =>
    act(async () => {
      await new Promise(resolve => setTimeout(resolve, 300));
    });

  it('treats a picked mention that ends the starting text as completed; a new @ still triggers', async () => {
    const user = userEvent.setup();
    render(<Harness initialValue="Ping @Jamie Okafor" initialMentions={[JAMIE]} />);
    const input = field();

    focusAtEnd(input);
    await pastDebounce();
    // Settled, as right after a pick: no search, no list, nothing announced.
    expect(mockedApi.searchMentionableUsers).not.toHaveBeenCalled();
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByRole('status').textContent).toBe('');

    await user.type(input, ' and @ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledTimes(1);
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('ja', 8);
  });

  it('still searches a trailing @name in the starting text that no chip names', async () => {
    render(<Harness initialValue="Ping @Jamie Okafor" />);
    const input = field();

    focusAtEnd(input);
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('Jamie Okafor', 8);
  });

  it('ArrowDown + Enter inserts the name and adds a removable chip', async () => {
    const user = userEvent.setup();
    const onMentions = jest.fn();
    render(<Harness onMentions={onMentions} />);
    const input = field();
    await user.type(input, 'ping @ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));

    await user.keyboard('{ArrowDown}');
    const options = within(listboxOf(input)).getAllByRole('option');
    expect(input).toHaveAttribute('aria-activedescendant', options[1].id);
    expect(options[1]).toHaveAttribute('aria-selected', 'true');

    await user.keyboard('{Enter}');

    expect(input.value).toBe('ping @Jana Whitfield ');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(onMentions).toHaveBeenLastCalledWith([JANA]);
    const chips = within(screen.getByRole('list', { name: 'Mentioned people' })).getAllByRole('button');
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveAccessibleName('Remove mention of Jana Whitfield');
    expect(screen.getByRole('status')).toHaveTextContent('Mentioned Jana Whitfield.');
    // The caret lands after the inserted name, so typing continues the sentence.
    expect(input.selectionStart).toBe('ping @Jana Whitfield '.length);
  });

  it('Tab picks the active person and keeps focus in the field', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = field();
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));

    await user.keyboard('{Tab}');

    expect(input.value).toBe('@Jamie Okafor ');
    expect(input).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();
  });

  it('clicking an option picks it', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = field();
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    await user.click(within(listboxOf(input)).getByRole('option', { name: 'Jamie Okafor' }));
    expect(input.value).toBe('@Jamie Okafor ');
    expect(input).toHaveFocus();
  });

  it('Escape closes only the list — an enclosing Modal stays open', async () => {
    const user = userEvent.setup();
    const onClose = jest.fn();
    render(
      <Modal open onClose={onClose} ariaLabel="Comment dialog">
        <Harness />
      </Modal>
    );
    const input = field();
    await user.click(input);
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));

    await user.keyboard('{Escape}');

    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // The same trigger stays closed while it is still being typed…
    await user.type(input, 'm');
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 300));
    });
    expect(input).toHaveAttribute('aria-expanded', 'false');

    // …and with the list closed, Escape belongs to the dialog again.
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('removing a chip leaves the text; deleting the text leaves the chip', async () => {
    const user = userEvent.setup();
    render(<Harness initialValue="ask @Jamie Okafor about it" initialMentions={[JAMIE, JANA]} />);
    const input = field();

    // Deleting the @Name text does NOT remove the chip.
    fireEvent.change(input, { target: { value: 'ask about it' } });
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();

    // Removing the chip does NOT touch the text, and focus moves to the next chip.
    await user.click(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' }));
    expect(screen.queryByRole('button', { name: 'Remove mention of Jamie Okafor' })).not.toBeInTheDocument();
    expect(input.value).toBe('ask about it');
    const remaining = screen.getByRole('button', { name: 'Remove mention of Jana Whitfield' });
    expect(remaining).toHaveFocus();
    expect(screen.getByRole('status')).toHaveTextContent('Removed mention of Jamie Okafor.');

    // Removing the last chip returns focus to the field.
    await user.click(remaining);
    expect(screen.queryByRole('list', { name: 'Mentioned people' })).not.toBeInTheDocument();
    expect(input).toHaveFocus();
  });

  it('picking someone already mentioned inserts text but adds no duplicate chip', async () => {
    const user = userEvent.setup();
    render(<Harness initialMentions={[JAMIE]} />);
    const input = field();
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(within(listboxOf(input)).getByRole('option', { name: /Jamie Okafor/ })).toHaveTextContent('mentioned');
    await user.keyboard('{Enter}');
    expect(input.value).toBe('@Jamie Okafor ');
    expect(screen.getAllByRole('button', { name: /Remove mention of/ })).toHaveLength(1);
  });

  it('refuses a mention past the cap and says why', async () => {
    const user = userEvent.setup();
    render(<Harness initialMentions={[{ id: 99, display_name: 'Rory Castellan' }]} maxMentions={1} />);
    const input = field();
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    expect(input.value).toBe('@ja');
    expect(screen.getAllByRole('button', { name: /Remove mention of/ })).toHaveLength(1);
    expect(screen.getByRole('alert')).toHaveTextContent('You can mention at most 1 people in one comment.');
  });

  it('drops a refused-mention error when the caller resets the field (a successful post)', async () => {
    const user = userEvent.setup();
    render(<Harness initialMentions={[{ id: 99, display_name: 'Rory Castellan' }]} maxMentions={1} withReset />);
    const input = field();
    await user.type(input, '@ja');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('You can mention at most 1 people in one comment.');
    expect(field()).toHaveAttribute('aria-invalid', 'true');

    // The caller clears value AND mentions without the field's own onChange.
    await user.click(screen.getByRole('button', { name: 'Reset field' }));
    expect(field().value).toBe('');
    expect(screen.queryByRole('list', { name: 'Mentioned people' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(field()).not.toHaveAttribute('aria-invalid');
  });

  it('shows a counter against the 5000 limit', () => {
    render(<Harness initialValue="  twelve chars  " />);
    expect(screen.getByText('12 / 5000')).toBeInTheDocument();
    expect(field()).toHaveAccessibleDescription(/Type @ and a name to mention someone\. 12 \/ 5000/);
  });

  it('Ctrl+Enter submits when the list is closed', async () => {
    const user = userEvent.setup();
    const onSubmitShortcut = jest.fn();
    render(<Harness onSubmitShortcut={onSubmitShortcut} />);
    await user.type(field(), 'ready');
    await user.keyboard('{Control>}{Enter}{/Control}');
    expect(onSubmitShortcut).toHaveBeenCalledTimes(1);
    expect(field().value).toBe('ready');
  });

  it('announces a failed search instead of failing silently', async () => {
    mockedApi.searchMentionableUsers.mockRejectedValueOnce(new Error('offline'));
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(field(), '@ja');
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Could not search people.'));
  });
});
