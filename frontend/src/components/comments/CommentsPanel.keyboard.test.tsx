/**
 * CommentsPanel — the Ctrl+Enter shortcut, which the main suite never presses
 * (every post / save there goes through a button).
 *
 * Pinned here:
 *  - Ctrl+Enter in the composer posts — exactly ONCE even when pressed again
 *    while the request is in flight (the composer is read-only, not disabled,
 *    so it keeps focus and still receives the second keydown);
 *  - Ctrl+Enter on a whitespace-only draft posts nothing, like the disabled
 *    Post button;
 *  - Ctrl+Enter in an inline editor saves the FULL new state, and does nothing
 *    while the edit is unchanged.
 */

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import CommentsPanel from './CommentsPanel';
import type { CommentResponse, WatchState } from '../../types/comments';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listComments: jest.fn(),
    createComment: jest.fn(),
    updateComment: jest.fn(),
    deleteComment: jest.fn(),
    getWatchState: jest.fn(),
    watchRecord: jest.fn(),
    unwatchRecord: jest.fn(),
    searchMentionableUsers: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const AVERY = { id: 3, display_name: 'Avery Lind' };
const JAMIE = { id: 5, display_name: 'Jamie Okafor' };

function makeComment(overrides: Partial<CommentResponse> = {}): CommentResponse {
  return {
    id: 1,
    entity_type: 'work_order',
    entity_id: 42,
    author: AVERY,
    body: 'Material is staged at the brake.',
    created_at: '2026-11-16T15:30:00Z',
    edited_at: null,
    mentions: [],
    can_edit: true,
    can_delete: true,
    ...overrides,
  };
}

const NOT_WATCHING: WatchState = { watching: false, watcher_count: 0 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderPanel() {
  return render(
    <ToastProvider>
      <CommentsPanel entityType="work_order" entityId={42} />
    </ToastProvider>
  );
}

const composer = () => screen.getByRole('combobox', { name: /add a comment/i }) as HTMLTextAreaElement;

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.listComments.mockResolvedValue([makeComment()]);
  mockedApi.getWatchState.mockResolvedValue(NOT_WATCHING);
  mockedApi.searchMentionableUsers.mockResolvedValue([JAMIE]);
});

describe('Ctrl+Enter', () => {
  it('posts once from the composer, even when pressed again while the post is in flight', async () => {
    const user = userEvent.setup();
    const created = deferred<CommentResponse>();
    mockedApi.createComment.mockReturnValueOnce(created.promise);
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    await user.type(composer(), 'Bend radius checked');
    await user.keyboard('{Control>}{Enter}{/Control}');
    await user.keyboard('{Control>}{Enter}{/Control}');

    expect(mockedApi.createComment).toHaveBeenCalledTimes(1);
    expect(mockedApi.createComment).toHaveBeenCalledWith({
      entity_type: 'work_order',
      entity_id: 42,
      body: 'Bend radius checked',
      mentioned_user_ids: [],
    });
    expect(composer()).toHaveFocus();
    expect(composer().value).toBe('Bend radius checked');

    await act(async () => created.resolve(makeComment({ id: 2, body: 'Bend radius checked' })));
    await waitFor(() => expect(composer().value).toBe(''));
    expect(mockedApi.createComment).toHaveBeenCalledTimes(1);
  });

  it('posts nothing for a whitespace-only draft', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    await user.type(composer(), '   ');
    await user.keyboard('{Control>}{Enter}{/Control}');

    expect(mockedApi.createComment).not.toHaveBeenCalled();
  });

  it('saves an inline edit with the full new state, and nothing while it is unchanged', async () => {
    const user = userEvent.setup();
    const withMention = makeComment({ body: 'Ping @Jamie Okafor', mentions: [JAMIE] });
    mockedApi.listComments.mockResolvedValue([withMention]);
    mockedApi.updateComment.mockResolvedValueOnce({
      ...withMention,
      body: 'Ping @Jamie Okafor today',
      edited_at: '2026-11-16T18:00:00Z',
    });
    renderPanel();
    await screen.findByText('Ping @Jamie Okafor');

    await user.click(screen.getByRole('button', { name: /^Edit comment/ }));
    const editor = screen.getByRole('combobox', { name: /edit comment/i }) as HTMLTextAreaElement;
    await user.click(editor);
    await user.keyboard('{Control>}{Enter}{/Control}');
    expect(mockedApi.updateComment).not.toHaveBeenCalled();

    await user.type(editor, ' today');
    await user.keyboard('{Control>}{Enter}{/Control}');

    expect(mockedApi.updateComment).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateComment).toHaveBeenCalledWith(1, {
      body: 'Ping @Jamie Okafor today',
      mentioned_user_ids: [5],
    });
    await waitFor(() => expect(screen.queryByRole('combobox', { name: /edit comment/i })).not.toBeInTheDocument());
    expect(mockedApi.createComment).not.toHaveBeenCalled();
  });
});
