/**
 * CommentsPanel — the work-order comment thread, composer, inline edit,
 * delete and Watch toggle.
 *
 * Pinned here:
 *  - the THREAD renders author, Central time, an "edited" marker carrying the
 *    edited time, the body as PLAIN TEXT (markup stays text, whitespace kept)
 *    and mention chips; Edit/Delete come from the server's `can_edit` /
 *    `can_delete`, never from a client role check;
 *  - writes are NON-OPTIMISTIC: a posted comment appears only once the server
 *    returns it, and a refusal keeps the draft and shows the server's detail;
 *  - POST sends the structured `mentioned_user_ids`; PATCH sends the FULL new
 *    state; DELETE goes through a ConfirmDialog whose `pending` locks it;
 *  - the Watch toggle is a real toggle (`aria-pressed`) and the watch state is
 *    re-read after posting (the server auto-watches commenters);
 *  - the panel's own 30 s poll refreshes the thread, skips hidden tabs, and
 *    never clobbers a draft or an open edit.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import CommentsPanel, { COMMENTS_POLL_INTERVAL_MS } from './CommentsPanel';
import { formatCentralDateTime } from '../../utils/centralTime';
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
const RORY = { id: 8, display_name: 'Rory Castellan' };

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

const OWN = makeComment();
const OTHERS = makeComment({
  id: 2,
  author: RORY,
  body: 'First article\n  passed <b>flatness</b> check',
  created_at: '2026-11-16T16:45:00Z',
  edited_at: '2026-11-16T17:05:00Z',
  mentions: [JAMIE],
  can_edit: false,
  can_delete: false,
});

const NOT_WATCHING: WatchState = { watching: false, watcher_count: 1 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderPanel() {
  return render(
    <ToastProvider>
      <CommentsPanel entityType="work_order" entityId={42} />
    </ToastProvider>
  );
}

const composer = () => screen.getByRole('combobox', { name: /add a comment/i }) as HTMLTextAreaElement;
const thread = () => screen.getByRole('list', { name: 'Comments' });
const commentItem = (text: string | RegExp) => {
  const item = within(thread())
    .getAllByRole('listitem')
    .find(li => li.parentElement === thread() && (typeof text === 'string' ? li.textContent?.includes(text) : text.test(li.textContent ?? '')));
  if (!item) throw new Error(`No comment containing ${String(text)}`);
  return item;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.listComments.mockResolvedValue([OWN, OTHERS]);
  mockedApi.getWatchState.mockResolvedValue(NOT_WATCHING);
  mockedApi.searchMentionableUsers.mockResolvedValue([JAMIE]);
});

afterEach(() => {
  jest.useRealTimers();
});

describe('thread', () => {
  it('renders author, Central time, the edited marker and the body as plain text', async () => {
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    expect(mockedApi.listComments).toHaveBeenCalledWith('work_order', 42);
    expect(mockedApi.getWatchState).toHaveBeenCalledWith('work_order', 42);

    const own = commentItem('Material is staged');
    expect(within(own).getByText('Avery Lind')).toBeInTheDocument();
    const time = within(own).getByText(formatCentralDateTime(OWN.created_at));
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('dateTime', OWN.created_at);
    expect(within(own).queryByText(/edited/)).not.toBeInTheDocument();

    const others = commentItem('First article');
    expect(within(others).getByText('Rory Castellan')).toBeInTheDocument();
    expect(within(others).getByTitle(`Edited ${formatCentralDateTime(OTHERS.edited_at)}`)).toHaveTextContent('(edited');
    // Plain text: markup stays literal text (no <b> element), whitespace is kept.
    const body = within(others).getByText(/First article/);
    expect(body.textContent).toBe('First article\n  passed <b>flatness</b> check');
    expect(body.querySelector('b')).toBeNull();
    expect(body).toHaveClass('whitespace-pre-wrap');
    expect(within(within(others).getByRole('list', { name: 'Mentioned' })).getByText('@Jamie Okafor')).toBeInTheDocument();
  });

  it('offers Edit and Delete only where the server says so', async () => {
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    const own = commentItem('Material is staged');
    expect(within(own).getByRole('button', { name: /^Edit comment/ })).toBeInTheDocument();
    expect(within(own).getByRole('button', { name: /^Delete comment/ })).toBeInTheDocument();

    const others = commentItem('First article');
    expect(within(others).queryByRole('button', { name: /^Edit comment/ })).not.toBeInTheDocument();
    expect(within(others).queryByRole('button', { name: /^Delete comment/ })).not.toBeInTheDocument();
  });

  it('lets a manager delete someone else’s comment without being able to edit it', async () => {
    mockedApi.listComments.mockResolvedValue([{ ...OTHERS, can_delete: true }]);
    renderPanel();
    await screen.findByText(/First article/);
    const others = commentItem('First article');
    expect(within(others).queryByRole('button', { name: /^Edit comment/ })).not.toBeInTheDocument();
    expect(within(others).getByRole('button', { name: /^Delete comment by Rory Castellan/ })).toBeInTheDocument();
  });

  it('shows a skeleton while loading, then an empty state', async () => {
    const list = deferred<CommentResponse[]>();
    mockedApi.listComments.mockReturnValueOnce(list.promise);
    renderPanel();
    expect(screen.getByLabelText('Loading comments')).toHaveAttribute('aria-busy', 'true');
    await act(async () => list.resolve([]));
    expect(screen.getByText('No comments yet')).toBeInTheDocument();
    expect(screen.queryByLabelText('Loading comments')).not.toBeInTheDocument();
  });

  it('shows an ErrorState with Retry when the thread fails to load', async () => {
    mockedApi.listComments.mockRejectedValueOnce({ response: { status: 404, data: { detail: 'Work order not found' } } });
    renderPanel();
    expect(await screen.findByText("Couldn't load comments")).toBeInTheDocument();
    expect(screen.getByText('Work order not found')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Material is staged at the brake.')).toBeInTheDocument();
    expect(mockedApi.listComments).toHaveBeenCalledTimes(2);
  });
});

describe('posting', () => {
  it('posts the body with structured mention ids and shows it only once the server returns it', async () => {
    const user = userEvent.setup();
    const created = deferred<CommentResponse>();
    mockedApi.createComment.mockReturnValueOnce(created.promise);
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    await user.type(composer(), 'Can you check @jam');
    await waitFor(() => expect(composer()).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    await user.type(composer(), 'the bend radius?');
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Post comment' }));
    expect(mockedApi.createComment).toHaveBeenCalledWith({
      entity_type: 'work_order',
      entity_id: 42,
      body: 'Can you check @Jamie Okafor the bend radius?',
      mentioned_user_ids: [5],
    });
    // Non-optimistic: nothing new in the thread while the request is in flight.
    expect(within(thread()).queryByText(/bend radius/)).not.toBeInTheDocument();
    expect(composer().value).toBe('Can you check @Jamie Okafor the bend radius?');

    mockedApi.getWatchState.mockResolvedValueOnce({ watching: true, watcher_count: 3 });
    await act(async () =>
      created.resolve(
        makeComment({ id: 3, body: 'Can you check @Jamie Okafor the bend radius?', mentions: [JAMIE] })
      )
    );

    expect(within(thread()).getByText('Can you check @Jamie Okafor the bend radius?')).toBeInTheDocument();
    expect(composer().value).toBe('');
    expect(screen.queryByRole('button', { name: 'Remove mention of Jamie Okafor' })).not.toBeInTheDocument();
    // The server auto-watched the poster: the toggle re-reads it.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Watch' })).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.getByText('3 watching')).toBeInTheDocument();
    expect(mockedApi.getWatchState).toHaveBeenCalledTimes(2);
    expect(composer()).toHaveFocus();
  });

  it('shows the server’s 422 detail and keeps the draft and its mentions', async () => {
    const user = userEvent.setup();
    const detail = 'These people cannot be mentioned on this work order: 5';
    mockedApi.createComment.mockRejectedValueOnce({ response: { status: 422, data: { detail } } });
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    await user.type(composer(), 'hey @jam');
    await waitFor(() => expect(composer()).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    await user.click(screen.getByRole('button', { name: 'Post comment' }));

    // Toast (role=alert) and the field's own error both carry the detail.
    await waitFor(() => expect(screen.getAllByText(detail)).toHaveLength(2));
    expect(screen.getAllByRole('alert').some(el => el.textContent?.includes(detail))).toBe(true);
    expect(composer().value).toBe('hey @Jamie Okafor ');
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();
    expect(within(thread()).getAllByRole('listitem').filter(li => li.parentElement === thread())).toHaveLength(2);
  });

  it('keeps Post disabled for an empty or whitespace-only draft', async () => {
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    expect(screen.getByRole('button', { name: 'Post comment' })).toBeDisabled();
    fireEvent.change(composer(), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Post comment' })).toBeDisabled();
    fireEvent.change(composer(), { target: { value: 'ok' } });
    expect(screen.getByRole('button', { name: 'Post comment' })).toBeEnabled();
  });
});

describe('unsaved-changes guard', () => {
  const unloadBlocked = () => {
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };

  it('guards an unsent draft and releases the guard once it is posted', async () => {
    mockedApi.createComment.mockResolvedValueOnce(makeComment({ id: 4, body: 'Unsent thought' }));
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    expect(unloadBlocked()).toBe(false);

    fireEvent.change(composer(), { target: { value: 'Unsent thought' } });
    expect(unloadBlocked()).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Post comment' }));
    expect(await within(thread()).findByText('Unsent thought')).toBeInTheDocument();
    expect(unloadBlocked()).toBe(false);
  });

  it('guards an open edit only once it differs from the saved comment', async () => {
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    fireEvent.click(within(commentItem('Material is staged')).getByRole('button', { name: /^Edit comment/ }));
    expect(unloadBlocked()).toBe(false);
    fireEvent.change(screen.getByRole('combobox', { name: /edit comment/i }), { target: { value: 'Changed' } });
    expect(unloadBlocked()).toBe(true);
  });
});

describe('editing and deleting', () => {
  it('edits inline, prefilled, and PATCHes the full new state', async () => {
    const user = userEvent.setup();
    const withMention = makeComment({ body: 'Ping @Jamie Okafor', mentions: [JAMIE] });
    mockedApi.listComments.mockResolvedValue([withMention]);
    mockedApi.updateComment.mockResolvedValueOnce({
      ...withMention,
      body: 'Ping @Jamie Okafor — done',
      edited_at: '2026-11-16T18:00:00Z',
    });
    renderPanel();
    await screen.findByText('Ping @Jamie Okafor');

    await user.click(screen.getByRole('button', { name: /^Edit comment/ }));
    const editor = screen.getByRole('combobox', { name: /edit comment/i }) as HTMLTextAreaElement;
    expect(editor.value).toBe('Ping @Jamie Okafor');
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();
    // Unchanged → nothing to save.
    expect(screen.getByRole('button', { name: 'Save comment' })).toBeDisabled();

    fireEvent.change(editor, { target: { value: 'Ping @Jamie Okafor — done' } });
    await user.click(screen.getByRole('button', { name: 'Save comment' }));

    expect(mockedApi.updateComment).toHaveBeenCalledWith(1, {
      body: 'Ping @Jamie Okafor — done',
      mentioned_user_ids: [5],
    });
    expect(await within(thread()).findByText('Ping @Jamie Okafor — done')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: /edit comment/i })).not.toBeInTheDocument();
    expect(within(thread()).getByText(/\(edited/)).toBeInTheDocument();
    // Focus goes back to the comment's Edit button rather than <body>.
    expect(screen.getByRole('button', { name: /^Edit comment/ })).toHaveFocus();
  });

  it('removing a mention chip while editing sends the reduced list', async () => {
    const user = userEvent.setup();
    const withMention = makeComment({ body: 'Ping @Jamie Okafor', mentions: [JAMIE] });
    mockedApi.listComments.mockResolvedValue([withMention]);
    mockedApi.updateComment.mockResolvedValueOnce({ ...withMention, mentions: [] });
    renderPanel();
    await screen.findByText('Ping @Jamie Okafor');
    await user.click(screen.getByRole('button', { name: /^Edit comment/ }));
    await user.click(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' }));
    await user.click(screen.getByRole('button', { name: 'Save comment' }));
    expect(mockedApi.updateComment).toHaveBeenCalledWith(1, { body: 'Ping @Jamie Okafor', mentioned_user_ids: [] });
  });

  it('deletes through a ConfirmDialog that is locked while the request is pending', async () => {
    const user = userEvent.setup();
    const deletion = deferred<void>();
    mockedApi.deleteComment.mockReturnValueOnce(deletion.promise);
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    await user.click(within(commentItem('Material is staged')).getByRole('button', { name: /^Delete comment/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('Delete comment?')).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Delete comment' }));
    expect(mockedApi.deleteComment).toHaveBeenCalledWith(1);
    // pending: Cancel disabled, confirm shows its loading state, Escape refused.
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: /Delete comment/ })).toBeDisabled();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Non-optimistic: still listed until the server answers.
    expect(within(thread()).getByText('Material is staged at the brake.')).toBeInTheDocument();

    await act(async () => deletion.resolve());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText('Material is staged at the brake.')).not.toBeInTheDocument();
  });

  it('a refused delete toasts the server detail and keeps the comment', async () => {
    const user = userEvent.setup();
    mockedApi.deleteComment.mockRejectedValueOnce({ response: { status: 403, data: { detail: 'Not your comment' } } });
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    await user.click(within(commentItem('Material is staged')).getByRole('button', { name: /^Delete comment/ }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete comment' }));
    expect(await screen.findByText('Not your comment')).toBeInTheDocument();
    expect(within(thread()).getByText('Material is staged at the brake.')).toBeInTheDocument();
  });
});

describe('watching', () => {
  it('toggles watch with aria-pressed and the server’s count', async () => {
    const user = userEvent.setup();
    mockedApi.watchRecord.mockResolvedValueOnce({ watching: true, watcher_count: 2 });
    mockedApi.unwatchRecord.mockResolvedValueOnce({ watching: false, watcher_count: 1 });
    renderPanel();
    const toggle = await screen.findByRole('button', { name: 'Watch' });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByText('1 watching')).toBeInTheDocument();

    await user.click(toggle);
    expect(mockedApi.watchRecord).toHaveBeenCalledWith('work_order', 42);
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.getByText('2 watching')).toBeInTheDocument();

    await user.click(toggle);
    expect(mockedApi.unwatchRecord).toHaveBeenCalledWith('work_order', 42);
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'false'));
    expect(screen.getByText('1 watching')).toBeInTheDocument();
  });

  it('a refused toggle toasts and keeps the previous state', async () => {
    const user = userEvent.setup();
    mockedApi.watchRecord.mockRejectedValueOnce({ response: { status: 403, data: { detail: 'Watching is not available' } } });
    renderPanel();
    const toggle = await screen.findByRole('button', { name: 'Watch' });
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);
    expect(await screen.findByText('Watching is not available')).toBeInTheDocument();
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
  });
});

describe('polling', () => {
  const setVisibility = (state: DocumentVisibilityState) =>
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });

  afterEach(() => setVisibility('visible'));

  it('refreshes the thread every 30 s without touching the draft', async () => {
    jest.useFakeTimers();
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    fireEvent.change(composer(), { target: { value: 'half-written thought' } });

    mockedApi.listComments.mockResolvedValueOnce([OWN, OTHERS, makeComment({ id: 9, body: 'New from the floor', author: JAMIE, can_edit: false, can_delete: false })]);
    await act(async () => {
      jest.advanceTimersByTime(COMMENTS_POLL_INTERVAL_MS);
    });

    expect(await screen.findByText('New from the floor')).toBeInTheDocument();
    expect(mockedApi.listComments).toHaveBeenCalledTimes(2);
    expect(composer().value).toBe('half-written thought');
  });

  it('does not clobber an open edit when a refresh lands', async () => {
    jest.useFakeTimers();
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    fireEvent.click(within(commentItem('Material is staged')).getByRole('button', { name: /^Edit comment/ }));
    const editor = screen.getByRole('combobox', { name: /edit comment/i }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: 'my unsaved edit' } });

    mockedApi.listComments.mockResolvedValueOnce([{ ...OWN, body: 'Changed elsewhere' }, OTHERS]);
    await act(async () => {
      jest.advanceTimersByTime(COMMENTS_POLL_INTERVAL_MS);
    });
    await waitFor(() => expect(mockedApi.listComments).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('combobox', { name: /edit comment/i }) as HTMLTextAreaElement).value).toBe(
      'my unsaved edit'
    );
  });

  it('skips the poll while the tab is hidden and refreshes when it becomes visible', async () => {
    jest.useFakeTimers();
    renderPanel();
    await screen.findByText('Material is staged at the brake.');

    setVisibility('hidden');
    await act(async () => {
      jest.advanceTimersByTime(COMMENTS_POLL_INTERVAL_MS * 2);
    });
    expect(mockedApi.listComments).toHaveBeenCalledTimes(1);

    setVisibility('visible');
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await waitFor(() => expect(mockedApi.listComments).toHaveBeenCalledTimes(2));
  });

  it('keeps the last thread on screen when a refresh fails', async () => {
    jest.useFakeTimers();
    renderPanel();
    await screen.findByText('Material is staged at the brake.');
    mockedApi.listComments.mockRejectedValueOnce(new Error('Network Error'));
    await act(async () => {
      jest.advanceTimersByTime(COMMENTS_POLL_INTERVAL_MS);
    });
    expect(await screen.findByText(/could not be refreshed/)).toBeInTheDocument();
    expect(screen.getByText('Material is staged at the brake.')).toBeInTheDocument();
  });
});
