import { act, renderHook } from '@testing-library/react';
import api from '../services/api';
import { useShopFloorProduction } from './useShopFloorProduction';

jest.mock('../services/api', () => ({ __esModule: true, default: { reportOperationProduction: jest.fn(), reduceOperationProduction: jest.fn() } }));
const post = api.reportOperationProduction as jest.Mock;
const reduce = api.reduceOperationProduction as jest.Mock;
const actor = { companyId: 1, operatorId: 7 };
const body = { quantity_complete_delta: 3, quantity_scrapped_delta: 1, scrap_reason: 'Porosity', notes: 'Batch A', source: 'desktop' };
const draft = { time_entry_id: 52, quantity: 3, notes: 'Batch A' };
const storageKey = 'werco:shop-floor-production:v1:1:7';

beforeEach(() => {
  jest.restoreAllMocks();
  post.mockReset();
  reduce.mockReset();
  sessionStorage.clear();
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
});

test('writes recovery identity before sending, blocks duplicate taps, then confirms and clears only that operation draft', async () => {
  let resolve!: (value: unknown) => void;
  post.mockImplementation(() => {
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).pending.body.request_id).toEqual(expect.any(String));
    return new Promise(done => { resolve = done; });
  });
  const { result } = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => { result.current.saveDraft(31, draft); result.current.saveDraft(32, { ...draft, quantity: 8 }); });
  let request!: Promise<unknown>;
  act(() => { request = result.current.submit(31, body); });
  expect(result.current.phase).toBe('saving');
  expect(result.current.mutationsBlocked).toBe(true);
  await act(async () => { await expect(result.current.submit(31, body)).rejects.toThrow('already saving'); });
  await act(async () => { resolve({ request_id: 'receipt', replayed: false }); await request; });
  expect(post).toHaveBeenCalledTimes(1);
  expect(result.current.phase).toBe('saved');
  expect(result.current.mutationsBlocked).toBe(false);
  expect(result.current.unconfirmed).toBeNull();
  expect(result.current.readDraft(31)).toBeNull();
  expect(result.current.readDraft(32)?.quantity).toBe(8);
  expect(JSON.parse(sessionStorage.getItem(storageKey)!).pending).toBeNull();
});

test('lost responses preserve original body and draft across re-login, and only explicit recovery replays the same ID', async () => {
  post.mockRejectedValueOnce(new Error('response lost')).mockResolvedValue({ replayed: true });
  const first = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => first.result.current.saveDraft(31, draft));
  await act(async () => { await first.result.current.submit(31, body).catch(() => undefined); });
  const original = post.mock.calls[0][1];
  expect(first.result.current.phase).toBe('not-confirmed');
  act(() => first.result.current.clearDraft(31));
  expect(first.result.current.readDraft(31)).toEqual(draft);
  first.unmount();
  const second = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  expect(second.result.current.unconfirmed?.body).toEqual(original);
  expect(second.result.current.readDraft(31)).toEqual(draft);
  expect(post).toHaveBeenCalledTimes(1);
  await act(async () => { await expect(second.result.current.submit(32, { ...body, quantity_complete_delta: 8 })).rejects.toThrow('Not confirmed'); });
  expect(post).toHaveBeenCalledTimes(1);
  await act(async () => { await second.result.current.retry(); });
  expect(post.mock.calls[1]).toEqual([31, original]);
  expect(second.result.current.phase).toBe('saved');
  expect(second.result.current.readDraft(31)).toBeNull();
  await act(async () => { await second.result.current.submit(31, body); });
  expect(post.mock.calls[2][1].request_id).not.toBe(original.request_id);
});

test('drafts and uncertain requests are isolated by company and operator even without unmounting', async () => {
  post.mockRejectedValue(new Error('response lost'));
  const { result, rerender } = renderHook(props => useShopFloorProduction<typeof draft>(props), { initialProps: actor });
  act(() => result.current.saveDraft(31, draft));
  await act(async () => { await result.current.submit(31, body).catch(() => undefined); });
  rerender({ companyId: 1, operatorId: 8 });
  expect(result.current.readDraft(31)).toBeNull();
  expect(result.current.unconfirmed).toBeNull();
  await expect(result.current.retry()).rejects.toThrow('no unconfirmed');
  rerender({ companyId: 2, operatorId: 7 });
  expect(result.current.readDraft(31)).toBeNull();
  expect(result.current.unconfirmed).toBeNull();
  rerender(actor);
  expect(result.current.readDraft(31)).toEqual(draft);
  expect(result.current.unconfirmed?.operationId).toBe(31);
  expect(post).toHaveBeenCalledTimes(1);
});

test('initial refusal preserves the draft and corrected input gets a new request ID', async () => {
  post.mockRejectedValueOnce({ response: { status: 400, data: { detail: 'Too many pieces' } } }).mockResolvedValue({});
  const { result } = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => result.current.saveDraft(31, draft));
  await act(async () => { await result.current.submit(31, body).catch(() => undefined); });
  expect(result.current.phase).toBe('not-saved');
  expect(result.current.message).toBe('Not saved. Too many pieces');
  expect(result.current.unconfirmed).toBeNull();
  expect(result.current.readDraft(31)).toEqual(draft);
  await act(async () => { await result.current.submit(31, { ...body, quantity_complete_delta: 2 }); });
  expect(post.mock.calls[1][1].request_id).not.toBe(post.mock.calls[0][1].request_id);
});

test('expired session after uncertainty cannot discard the original report', async () => {
  post.mockRejectedValueOnce({ response: { status: 504 } }).mockRejectedValueOnce({ response: { status: 401 } }).mockResolvedValue({ replayed: true });
  const { result } = renderHook(() => useShopFloorProduction(actor));
  await act(async () => { await result.current.submit(31, body).catch(() => undefined); });
  const original = post.mock.calls[0][1];
  await act(async () => { await result.current.retry().catch(() => undefined); });
  expect(result.current.unconfirmed?.body).toEqual(original);
  expect(result.current.phase).toBe('not-confirmed');
  await act(async () => { await result.current.retry(); });
  expect(post.mock.calls.map(call => call[1])).toEqual([original, original, original]);
});

test('offline entry stays local and reconnect never submits automatically', async () => {
  const { result } = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => {
    result.current.saveDraft(31, draft);
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    window.dispatchEvent(new Event('offline'));
  });
  expect(result.current.online).toBe(false);
  expect(result.current.mutationsBlocked).toBe(true);
  await act(async () => { await expect(result.current.submit(31, body)).rejects.toThrow('Offline'); });
  expect(result.current.readDraft(31)).toEqual(draft);
  expect(result.current.unconfirmed).toBeNull();
  act(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
    window.dispatchEvent(new Event('online'));
  });
  expect(post).not.toHaveBeenCalled();
});

test('failure to persist request identity prevents an unsafe send', async () => {
  const { result } = renderHook(() => useShopFloorProduction(actor));
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage blocked'); });
  await act(async () => { await expect(result.current.submit(31, body)).rejects.toThrow('Production was not sent'); });
  expect(post).not.toHaveBeenCalled();
  expect(result.current.phase).toBe('not-saved');
});

test('old response after operator change never paints saved feedback for new operator', async () => {
  let resolve!: (value: unknown) => void;
  post.mockImplementation(() => new Promise(done => { resolve = done; }));
  const { result, rerender } = renderHook(props => useShopFloorProduction(props), { initialProps: actor });
  let request!: Promise<unknown>;
  act(() => { request = result.current.submit(31, body); });
  rerender({ companyId: 1, operatorId: 8 });
  await act(async () => { resolve({ replayed: false }); await request; });
  expect(result.current.phase).toBe('idle');
  expect(result.current.operationId).toBeNull();
  expect(JSON.parse(sessionStorage.getItem(storageKey)!).pending).toBeNull();
});

test('a retained submit callback cannot attribute the previous operator entry to a new login', async () => {
  const { result, rerender } = renderHook(props => useShopFloorProduction(props), { initialProps: actor });
  const previousSubmit = result.current.submit;
  rerender({ companyId: 1, operatorId: 8 });
  await expect(previousSubmit(31, body)).rejects.toThrow('original operator');
  expect(post).not.toHaveBeenCalled();
});

const correction = { quantity_delta: 2, reason: 'Count entered twice', notes: 'Original shift count', source: 'desktop' };

test('uncertain correction persists across re-login and blocks blind retry or conflicting production until explicit review', async () => {
  reduce.mockRejectedValueOnce(Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }));
  const first = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => first.result.current.saveDraft(31, draft));
  await act(async () => { await first.result.current.submitCorrection(31, correction).catch(() => undefined); });
  expect(first.result.current.phase).toBe('not-confirmed');
  expect(first.result.current.message).toContain('supervisor');
  expect(first.result.current.unconfirmed).toBeNull();
  expect(first.result.current.unconfirmedCorrection?.body).toEqual(correction);
  act(() => first.result.current.clearDraft(31));
  expect(first.result.current.readDraft(31)).toEqual(draft);
  first.unmount();
  const second = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  expect(second.result.current.mutationsBlocked).toBe(true);
  expect(second.result.current.unconfirmedCorrection?.body).toEqual(correction);
  await expect(second.result.current.submitCorrection(31, correction)).rejects.toThrow('Do not submit');
  await expect(second.result.current.submit(32, body)).rejects.toThrow('supervisor');
  await expect(second.result.current.retry()).rejects.toThrow('no unconfirmed report');
  expect(reduce).toHaveBeenCalledTimes(1);
  expect(post).not.toHaveBeenCalled();
  act(() => second.result.current.acknowledgeCorrectionReview());
  expect(second.result.current.mutationsBlocked).toBe(false);
  expect(second.result.current.readDraft(31)).toBeNull();
  expect(second.result.current.phase).toBe('idle');
  expect(JSON.parse(sessionStorage.getItem(storageKey)!).pendingCorrection).toBeNull();
  // Acknowledging review is not another production write.
  expect(reduce).toHaveBeenCalledTimes(1);
});

test('successful correction clears draft, whereas a definitive refusal keeps it editable', async () => {
  reduce.mockRejectedValueOnce({ response: { status: 400, data: { detail: 'Only one removable piece' } } }).mockResolvedValue({ message: 'Production quantity corrected' });
  const { result } = renderHook(() => useShopFloorProduction<typeof draft>(actor));
  act(() => result.current.saveDraft(31, draft));
  await act(async () => { await result.current.submitCorrection(31, correction).catch(() => undefined); });
  expect(result.current.phase).toBe('not-saved');
  expect(result.current.unconfirmedCorrection).toBeNull();
  expect(result.current.readDraft(31)).toEqual(draft);
  await act(async () => { await result.current.submitCorrection(31, { ...correction, quantity_delta: 1 }); });
  expect(result.current.phase).toBe('saved');
  expect(result.current.readDraft(31)).toBeNull();
});

test('correction review cannot clear another operator marker or unlock when storage is unavailable', async () => {
  reduce.mockRejectedValue(new Error('lost response'));
  const { result, rerender } = renderHook(props => useShopFloorProduction(props), { initialProps: actor });
  await act(async () => { await result.current.submitCorrection(31, correction).catch(() => undefined); });
  const oldAcknowledge = result.current.acknowledgeCorrectionReview;
  rerender({ companyId: 1, operatorId: 8 });
  expect(result.current.unconfirmedCorrection).toBeNull();
  expect(() => oldAcknowledge()).toThrow('original operator');
  rerender(actor);
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage blocked'); });
  expect(() => result.current.acknowledgeCorrectionReview()).toThrow('storage blocked');
  expect(result.current.mutationsBlocked).toBe(true);
});

test('correction intent is persisted before send and a second tap cannot subtract twice', async () => {
  let resolve!: (value: unknown) => void;
  reduce.mockImplementation(() => {
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).pendingCorrection.body).toEqual(correction);
    return new Promise(done => { resolve = done; });
  });
  const { result } = renderHook(() => useShopFloorProduction(actor));
  let request!: Promise<unknown>;
  act(() => { request = result.current.submitCorrection(31, correction); });
  expect(result.current.phase).toBe('saving');
  await expect(result.current.submitCorrection(31, correction)).rejects.toThrow('already saving');
  expect(() => result.current.acknowledgeCorrectionReview()).toThrow('Wait');
  await act(async () => { resolve({}); await request; });
  expect(reduce).toHaveBeenCalledTimes(1);
});
