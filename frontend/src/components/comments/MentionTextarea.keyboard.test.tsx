/**
 * MentionTextarea — keyboard edge cases the main suite leaves unpinned.
 *
 * `MentionTextarea.test.tsx` pins the happy paths (Up/Down + Enter/Tab picks,
 * Escape closing only the list, Ctrl+Enter submitting). This file pins the
 * edges of the same keyboard contract:
 *
 *  - Up/Down CLAMP at the ends of the list (no wrap), and the active option
 *    `aria-activedescendant` points at follows.
 *  - With the list closed, plain Enter is a newline (never a submit); Cmd+Enter
 *    submits like Ctrl+Enter.
 *  - Shift+Enter with the list open never picks.
 *  - Shift+Tab with the list open closes it WITHOUT picking and lets focus move
 *    on (backwards), leaving the text and the chips alone.
 *  - Escape while a search is still in flight (nothing shown yet) is NOT
 *    swallowed — an enclosing dialog closes as usual — and the dismissed
 *    trigger stays closed when the late result lands.
 *  - After Escape, a NEW `@` opens normally.
 *  - A pick in the middle of a sentence adds no double space and leaves the
 *    caret after the inserted name.
 *  - A pick that would overflow the character cap is refused, says why, and
 *    changes nothing.
 *  - A late response for a query the user has typed past is dropped (the list
 *    shows the newer query's people only).
 */

import React, { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import MentionTextarea from './MentionTextarea';
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
const JANE: CommentPerson = { id: 7, display_name: 'Jane Ruiz' };

interface HarnessProps {
  maxLength?: number;
  onSubmitShortcut?: () => void;
  /** A focusable control BEFORE the field, so Shift+Tab has somewhere to go. */
  withButtonBefore?: boolean;
}

function Harness({ maxLength, onSubmitShortcut, withButtonBefore = false }: HarnessProps) {
  const [value, setValue] = useState('');
  const [mentions, setMentions] = useState<CommentPerson[]>([]);
  return (
    <>
      {withButtonBefore && <button type="button">Before the field</button>}
      <MentionTextarea
        label="Add a comment"
        value={value}
        onChange={setValue}
        mentions={mentions}
        onMentionsChange={setMentions}
        maxLength={maxLength}
        onSubmitShortcut={onSubmitShortcut}
      />
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

const settle = () =>
  act(async () => {
    await new Promise(resolve => setTimeout(resolve, 300));
  });

const chipCount = () => screen.queryAllByRole('button', { name: /Remove mention of/ }).length;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.searchMentionableUsers.mockResolvedValue([JAMIE, JANA, JANE]);
});

async function openList(user: ReturnType<typeof userEvent.setup>, text = '@ja') {
  const input = field();
  await user.type(input, text);
  await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
  return input;
}

describe('MentionTextarea keyboard edges', () => {
  it('ArrowUp at the top and ArrowDown at the bottom clamp instead of wrapping', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = await openList(user);
    const options = () => within(listboxOf(input)).getAllByRole('option');

    await user.keyboard('{ArrowUp}');
    expect(input).toHaveAttribute('aria-activedescendant', options()[0].id);

    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}{ArrowDown}');
    expect(input).toHaveAttribute('aria-activedescendant', options()[2].id);
    expect(options()[2]).toHaveAttribute('aria-selected', 'true');
    expect(options().filter(option => option.getAttribute('aria-selected') === 'true')).toHaveLength(1);

    await user.keyboard('{Enter}');
    expect(input.value).toBe('@Jane Ruiz ');
    expect(screen.getByRole('button', { name: 'Remove mention of Jane Ruiz' })).toBeInTheDocument();
  });

  it('with the list closed, Enter is a newline and only Ctrl/Cmd+Enter submits', async () => {
    const user = userEvent.setup();
    const onSubmitShortcut = jest.fn();
    render(<Harness onSubmitShortcut={onSubmitShortcut} />);
    const input = field();

    await user.type(input, 'line one{Enter}line two');
    expect(input.value).toBe('line one\nline two');
    expect(onSubmitShortcut).not.toHaveBeenCalled();

    await user.keyboard('{Meta>}{Enter}{/Meta}');
    expect(onSubmitShortcut).toHaveBeenCalledTimes(1);
    expect(input.value).toBe('line one\nline two');
  });

  it('Shift+Enter with the list open never picks', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = await openList(user);

    await user.keyboard('{Shift>}{Enter}{/Shift}');

    expect(input.value).toBe('@ja\n');
    expect(chipCount()).toBe(0);
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('Shift+Tab with the list open closes it without picking and moves focus back', async () => {
    const user = userEvent.setup();
    render(<Harness withButtonBefore />);
    const input = await openList(user);

    await user.keyboard('{Shift>}{Tab}{/Shift}');

    expect(screen.getByRole('button', { name: 'Before the field' })).toHaveFocus();
    expect(input.value).toBe('@ja');
    expect(chipCount()).toBe(0);
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).not.toHaveAttribute('aria-controls');

    // Coming back to the field does not pop the dismissed trigger straight back open.
    await user.click(input);
    await settle();
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('Escape during an in-flight search belongs to the dialog, and the late result stays closed', async () => {
    const pending = deferred<CommentPerson[]>();
    mockedApi.searchMentionableUsers.mockReturnValueOnce(pending.promise);
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
    await waitFor(() => expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('ja', 8));
    expect(input).toHaveAttribute('aria-expanded', 'false'); // nothing shown yet

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve([JAMIE]);
      await pending.promise;
    });
    await settle();
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('after Escape, a new @ opens the list again', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = await openList(user);

    await user.keyboard('{Escape}');
    expect(input).toHaveAttribute('aria-expanded', 'false');

    await user.type(input, ' and @jan');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(mockedApi.searchMentionableUsers).toHaveBeenLastCalledWith('jan', 8);
    await user.keyboard('{Enter}');
    expect(input.value).toBe('@ja and @Jamie Okafor ');
  });

  it('a pick mid-sentence adds no double space and leaves the caret after the name', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = field();
    await user.type(input, 'ask  about it');
    input.setSelectionRange(4, 4);
    fireEvent.select(input);

    await user.type(input, '@ja', { skipClick: true, initialSelectionStart: 4, initialSelectionEnd: 4 });
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(mockedApi.searchMentionableUsers).toHaveBeenLastCalledWith('ja', 8);
    await user.keyboard('{Enter}');

    expect(input.value).toBe('ask @Jamie Okafor about it');
    expect(input.selectionStart).toBe('ask @Jamie Okafor '.length);
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('refuses a pick that would overflow the character cap, and changes nothing', async () => {
    const user = userEvent.setup();
    render(<Harness maxLength={10} />);
    const input = await openList(user);

    await user.keyboard('{Enter}');

    expect(input.value).toBe('@ja');
    expect(chipCount()).toBe(0);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'No room for this mention: a comment can be at most 10 characters.'
    );
    expect(input).toHaveAttribute('aria-expanded', 'false');
  });

  it('drops a late response for a query the user has already typed past', async () => {
    const slow = deferred<CommentPerson[]>();
    mockedApi.searchMentionableUsers.mockImplementation(query =>
      query === 'ja' ? slow.promise : Promise.resolve([JAMIE])
    );
    const user = userEvent.setup();
    render(<Harness />);
    const input = field();

    await user.type(input, '@ja');
    await waitFor(() => expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('ja', 8));
    await user.type(input, 'm');
    await waitFor(() => expect(input).toHaveAttribute('aria-expanded', 'true'));
    expect(
      within(listboxOf(input))
        .getAllByRole('option')
        .map(o => o.textContent)
    ).toEqual(['Jamie Okafor']);

    await act(async () => {
      slow.resolve([JANA, JANE]);
      await slow.promise;
    });

    expect(
      within(listboxOf(input))
        .getAllByRole('option')
        .map(o => o.textContent)
    ).toEqual(['Jamie Okafor']);
    await user.keyboard('{Enter}');
    expect(input.value).toBe('@Jamie Okafor ');
  });
});
