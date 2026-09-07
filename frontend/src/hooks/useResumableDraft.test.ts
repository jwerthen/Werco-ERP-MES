import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../services/api';
import { useResumableDraft } from './useResumableDraft';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { listWorkspaceRecords: jest.fn(), saveWorkspaceRecord: jest.fn(), deleteWorkspaceRecord: jest.fn() },
}));
const list = api.listWorkspaceRecords as jest.Mock;
const save = api.saveWorkspaceRecord as jest.Mock;
const remove = api.deleteWorkspaceRecord as jest.Mock;
const restore = jest.fn();
const saved = (note = 'Server draft', version = 1) => ({
  key: 'new',
  namespace: 'work-orders',
  kind: 'draft' as const,
  name: 'Unfinished draft',
  data: { note },
  version,
  updated_at: '2026-09-07T12:00:00',
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}
const valid = (data: unknown): data is { note: string } => typeof (data as { note?: string } | null)?.note === 'string';
const hook = (value = { note: 'My edits' }) =>
  renderHook(
    ({ note, enabled }) =>
      useResumableDraft({ namespace: 'work-orders', value: { note }, dirty: !!note, enabled, valid, restore }),
    { initialProps: { note: value.note, enabled: true } }
  );

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  list.mockResolvedValue([]);
  remove.mockResolvedValue(undefined);
  save.mockImplementation((_namespace, _key, data) => Promise.resolve(saved(data.data.note, data.version + 1)));
});
afterEach(() => {
  sessionStorage.clear();
  jest.useRealTimers();
});

test('offers the saved draft without overwriting current typing or autosaving over it', async () => {
  list.mockResolvedValue([saved()]);
  const { result } = hook();
  await waitFor(() => expect(result.current.candidate?.data.note).toBe('Server draft'));
  await act(async () => {
    await result.current.saveNow();
  });
  expect(restore).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
  act(() => result.current.resume());
  expect(restore).toHaveBeenCalledWith({ note: 'Server draft' });
  expect(result.current.blocked).toBe(false);
});

test('debounces autosave and persists only latest form entries', async () => {
  const { result, rerender } = hook({ note: '' });
  await waitFor(() => expect(result.current.ready).toBe(true));
  jest.useFakeTimers();
  rerender({ note: 'First', enabled: true });
  act(() => jest.advanceTimersByTime(400));
  rerender({ note: 'Latest', enabled: true });
  await act(async () => {
    jest.advanceTimersByTime(801);
  });
  expect(save).toHaveBeenCalledTimes(1);
  expect(save.mock.calls[0][2]).toMatchObject({ version: 0, data: { note: 'Latest' } });
});

test('serializes writes using the version returned by the previous write', async () => {
  const first = deferred<ReturnType<typeof saved>>();
  save.mockReturnValueOnce(first.promise);
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  let p1!: Promise<unknown>;
  let p2!: Promise<unknown>;
  act(() => {
    p1 = result.current.saveNow();
  });
  await act(async () => {
    await Promise.resolve();
  });
  rerender({ note: 'Second edit', enabled: true });
  act(() => {
    p2 = result.current.saveNow();
  });
  expect(save).toHaveBeenCalledTimes(1);
  await act(async () => {
    first.resolve(saved('My edits', 1));
    await p1;
    await p2;
  });
  expect(save.mock.calls[1][2]).toMatchObject({ version: 1, data: { note: 'Second edit' } });
});

test('successful business save waits for pending draft save and prevents resurrection', async () => {
  const pending = deferred<ReturnType<typeof saved>>();
  save.mockReturnValueOnce(pending.promise);
  const { result } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  let saving!: Promise<unknown>;
  let clearing!: Promise<boolean>;
  act(() => {
    saving = result.current.saveNow();
  });
  await act(async () => {
    await Promise.resolve();
  });
  act(() => {
    clearing = result.current.clear();
  });
  expect(remove).not.toHaveBeenCalled();
  await act(async () => {
    pending.resolve(saved('My edits', 1));
    await saving;
    expect(await clearing).toBe(true);
  });
  expect(remove).toHaveBeenCalledWith('work-orders', 'new', 'draft', 1);
  await act(async () => {
    await result.current.saveNow();
  });
  expect(save).toHaveBeenCalledTimes(1);
});

test('a conflict pauses autosave and reload offers the other version for review', async () => {
  save.mockRejectedValue({ response: { status: 409, data: { detail: 'Changed in another tab' } } });
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  await act(async () => {
    await result.current.saveNow().catch(() => undefined);
  });
  expect(result.current.error).toBe('Changed in another tab');
  rerender({ note: 'Still typing', enabled: true });
  await act(async () => {
    await result.current.saveNow();
  });
  expect(save).toHaveBeenCalledTimes(1);
  list.mockResolvedValue([saved('Other tab', 3)]);
  await act(async () => {
    await result.current.retry();
  });
  expect(result.current.candidate?.data.note).toBe('Other tab');
  expect(restore).not.toHaveBeenCalled();
});

test('malformed saved draft cannot crash the form through Resume', async () => {
  list.mockResolvedValue([{ ...saved(), data: { note: null } }]);
  const { result } = hook();
  await waitFor(() => expect(result.current.candidate).not.toBeNull());
  expect(result.current.canResume).toBe(false);
  act(() => result.current.resume());
  expect(restore).not.toHaveBeenCalled();
  await act(async () => {
    await result.current.discard();
  });
  expect(result.current.ready).toBe(true);
});

test('keeps the recoverable draft when removal fails', async () => {
  list.mockResolvedValue([saved()]);
  remove.mockRejectedValue(new Error('offline'));
  const { result } = hook();
  await waitFor(() => expect(result.current.candidate).not.toBeNull());
  await act(async () => {
    await result.current.discard();
  });
  expect(result.current.candidate).not.toBeNull();
  expect(result.current.error).toContain('could not be removed');
});

test('reopening a modal offers the draft it saved before closing', async () => {
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  await act(async () => {
    await result.current.saveNow();
  });
  rerender({ note: '', enabled: false });
  rerender({ note: '', enabled: true });
  await waitFor(() => expect(result.current.candidate?.data.note).toBe('My edits'));
});

test('erasing autosaved form entries saves the blank current form instead of resurrecting old input', async () => {
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  await act(async () => {
    await result.current.saveNow();
  });
  jest.useFakeTimers();
  rerender({ note: '', enabled: true });
  await act(async () => {
    jest.advanceTimersByTime(801);
  });
  expect(save).toHaveBeenCalledTimes(2);
  expect(save.mock.calls[1][2]).toMatchObject({ version: 1, data: { note: '' } });
});

test('a pristine form does not create an empty saved draft', async () => {
  const { result } = hook({ note: '' });
  await waitFor(() => expect(result.current.ready).toBe(true));
  jest.useFakeTimers();
  await act(async () => {
    jest.advanceTimersByTime(1601);
  });
  expect(save).not.toHaveBeenCalled();
});

test('blank edits made during the first save use its completed version', async () => {
  const pending = deferred<ReturnType<typeof saved>>();
  save.mockReturnValueOnce(pending.promise);
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  let saving!: Promise<unknown>;
  act(() => {
    saving = result.current.saveNow();
  });
  await waitFor(() => expect(result.current.busy).toBe(true));
  jest.useFakeTimers();
  rerender({ note: '', enabled: true });
  await act(async () => {
    pending.resolve(saved('My edits', 1));
    await saving;
  });
  await act(async () => {
    jest.advanceTimersByTime(801);
  });
  expect(save.mock.calls[1][2]).toMatchObject({ version: 1, data: { note: '' } });
});

test('reopening while the previous autosave is pending waits and offers its completed draft', async () => {
  const pending = deferred<ReturnType<typeof saved>>();
  save.mockReturnValueOnce(pending.promise);
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  let saving!: Promise<unknown>;
  act(() => {
    saving = result.current.saveNow();
  });
  await waitFor(() => expect(result.current.busy).toBe(true));
  rerender({ note: '', enabled: false });
  rerender({ note: '', enabled: true });
  await act(async () => {
    pending.resolve(saved('My edits', 1));
    await saving;
  });
  expect(result.current.busy).toBe(false);
  expect(result.current.candidate?.data.note).toBe('My edits');
  expect(result.current.blocked).toBe(true);
  expect(save).toHaveBeenCalledTimes(1);
});

test('an old account save response cannot become the next account recovery candidate', async () => {
  const pending = deferred<ReturnType<typeof saved>>();
  save.mockReturnValueOnce(pending.promise);
  const { result, rerender } = hook();
  await waitFor(() => expect(result.current.ready).toBe(true));
  let saving!: Promise<unknown>;
  act(() => {
    saving = result.current.saveNow();
  });
  await waitFor(() => expect(result.current.busy).toBe(true));
  list.mockResolvedValue([saved('New account draft', 4)]);
  sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 2 }));
  rerender({ note: '', enabled: true });
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  await act(async () => {
    pending.resolve(saved('Old account private text', 1));
    await saving;
  });
  await waitFor(() => expect(result.current.candidate?.data.note).toBe('New account draft'));
  expect(result.current.busy).toBe(false);
  expect(result.current.error).toBe('');
  expect(restore).not.toHaveBeenCalled();
});
