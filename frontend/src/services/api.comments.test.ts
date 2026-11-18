/**
 * Comments / watchers / mention-search client boundary — the wire shapes,
 * pinned at their source.
 *
 * The CommentsPanel and MentionTextarea suites mock `services/api`, so they
 * assert against the DECLARED return types of these methods, never against
 * the URLs and bodies actually sent. This file pins the half they cannot see:
 *
 *  1. Reads go to `/comments` and `/watchers` with `entity_type` / `entity_id`
 *     as QUERY params (not path segments).
 *  2. `createComment` posts the structured mention list as `mentioned_user_ids`
 *     — the server never parses names out of the text, so this list IS the
 *     mention.
 *  3. `updateComment` PATCHes the FULL new state (both fields), never a diff.
 *  4. `unwatchRecord` is a DELETE with query params (the server takes no body
 *     there) and, like `watchRecord`, returns the new watch state.
 *  5. `searchMentionableUsers` hits `/users/mention-search` (declared above
 *     `/users/{id}` server-side) with `q` and `limit`.
 *
 * axios is mocked at the module boundary (same pattern as api.spc.test.ts).
 */

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
  patch: mockPatch,
  delete: mockDelete,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: jest.fn() },
    create,
  };
});

import api from './api';
import type { CommentResponse, WatchState } from '../types/comments';

const ok = (data: unknown, status = 200) => ({ status, data, headers: {} });

const comment: CommentResponse = {
  id: 9,
  entity_type: 'work_order',
  entity_id: 42,
  author: { id: 3, display_name: 'Avery Lind' },
  body: 'Check the flatness on the second piece.',
  created_at: '2026-11-16T15:30:00Z',
  edited_at: null,
  mentions: [{ id: 5, display_name: 'Jamie Okafor' }],
  can_edit: true,
  can_delete: true,
};

const watchState: WatchState = { watching: true, watcher_count: 2 };

beforeEach(() => {
  jest.clearAllMocks();
});

describe('comments client', () => {
  it('lists a record’s comments with entity_type / entity_id as query params', async () => {
    mockGet.mockResolvedValueOnce(ok([comment]));
    await expect(api.listComments('work_order', 42)).resolves.toEqual([comment]);
    expect(mockGet).toHaveBeenCalledWith('/comments', { params: { entity_type: 'work_order', entity_id: 42 } });
  });

  it('posts a comment with the structured mention id list', async () => {
    mockPost.mockResolvedValueOnce(ok(comment, 201));
    const input = {
      entity_type: 'work_order' as const,
      entity_id: 42,
      body: 'Check the flatness on the second piece.',
      mentioned_user_ids: [5],
    };
    await expect(api.createComment(input)).resolves.toEqual(comment);
    expect(mockPost).toHaveBeenCalledWith('/comments', input);
  });

  it('PATCHes the full new body and mention list', async () => {
    mockPatch.mockResolvedValueOnce(ok({ ...comment, edited_at: '2026-11-16T16:00:00Z' }));
    await api.updateComment(9, { body: 'Edited', mentioned_user_ids: [] });
    expect(mockPatch).toHaveBeenCalledWith('/comments/9', { body: 'Edited', mentioned_user_ids: [] });
  });

  it('deletes a comment and resolves to nothing on 204', async () => {
    mockDelete.mockResolvedValueOnce({ status: 204, data: '', headers: {} });
    await expect(api.deleteComment(9)).resolves.toBeUndefined();
    expect(mockDelete).toHaveBeenCalledWith('/comments/9');
  });
});

describe('watchers client', () => {
  it('reads the watch state with query params', async () => {
    mockGet.mockResolvedValueOnce(ok(watchState));
    await expect(api.getWatchState('work_order', 42)).resolves.toEqual(watchState);
    expect(mockGet).toHaveBeenCalledWith('/watchers', { params: { entity_type: 'work_order', entity_id: 42 } });
  });

  it('watches with a JSON body and returns the new state', async () => {
    mockPost.mockResolvedValueOnce(ok(watchState));
    await expect(api.watchRecord('work_order', 42)).resolves.toEqual(watchState);
    expect(mockPost).toHaveBeenCalledWith('/watchers', { entity_type: 'work_order', entity_id: 42 });
  });

  it('unwatches with a DELETE carrying query params and returns the new state', async () => {
    mockDelete.mockResolvedValueOnce(ok({ watching: false, watcher_count: 1 }));
    await expect(api.unwatchRecord('work_order', 42)).resolves.toEqual({ watching: false, watcher_count: 1 });
    expect(mockDelete).toHaveBeenCalledWith('/watchers', { params: { entity_type: 'work_order', entity_id: 42 } });
  });
});

describe('mention search client', () => {
  it('searches /users/mention-search with q and the default limit', async () => {
    mockGet.mockResolvedValueOnce(ok([{ id: 5, display_name: 'Jamie Okafor' }]));
    await expect(api.searchMentionableUsers('ja')).resolves.toEqual([{ id: 5, display_name: 'Jamie Okafor' }]);
    expect(mockGet).toHaveBeenCalledWith('/users/mention-search', { params: { q: 'ja', limit: 10 } });
  });

  it('passes an explicit limit through', async () => {
    mockGet.mockResolvedValueOnce(ok([]));
    await api.searchMentionableUsers('ok', 25);
    expect(mockGet).toHaveBeenCalledWith('/users/mention-search', { params: { q: 'ok', limit: 25 } });
  });
});
