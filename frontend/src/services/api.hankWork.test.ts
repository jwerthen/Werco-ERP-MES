/** Public Hank HTTP contracts, including real Axios multipart transformation. */
import type { AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios';
import type { HankIntakePlan } from '../types/hankIntake';
import type { HankHandoffCreate, HankRoutineAdvance, HankRoutineValues } from '../types/hankWork';

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
  delete: jest.fn(),
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};
const mockCreate = jest.fn((_config?: AxiosRequestConfig) => mockAxiosInstance);

jest.mock('axios', () => ({
  __esModule: true,
  default: { create: mockCreate, post: jest.fn() },
  create: mockCreate,
}));

import api from './api';

const command = Object.freeze({ expected_company_id: 17, expected_version: 8 });
const requestKey = '99f1e982-3d9a-4d49-8b53-f5d4c94b3571';
const serverResult = Object.freeze({ id: 42, company_id: 17, version: 9, status: 'completed' });
let signal: AbortSignal;

beforeEach(() => {
  mockGet.mockReset().mockResolvedValue({ data: serverResult });
  mockPost.mockReset().mockResolvedValue({ data: serverResult });
  mockPut.mockReset().mockResolvedValue({ data: serverResult });
  signal = new AbortController().signal;
});

describe('operational evidence and saved-work reads', () => {
  it.each([
    [
      'readiness',
      (id: number, cancellation: AbortSignal) => api.getHankReadiness(id, cancellation),
      '/hank/work-orders/42/readiness',
    ],
    [
      'knowledge',
      (id: number, cancellation: AbortSignal) => api.getHankKnowledge(id, cancellation),
      '/hank/work-orders/42/knowledge',
    ],
    [
      'purchasing impact',
      (id: number, cancellation: AbortSignal) => api.getHankPurchasingImpact(id, cancellation),
      '/hank/purchase-orders/42/impact',
    ],
    [
      'shipping packet',
      (id: number, cancellation: AbortSignal) => api.getHankShippingPacket(id, cancellation),
      '/hank/work-orders/42/shipping-packet',
    ],
  ] as const)('reads %s with its cancellation signal and returns the server evidence', async (_, read, url) => {
    expect(await read(42, signal)).toBe(serverResult);
    expect(mockGet).toHaveBeenCalledWith(url, { signal });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('reads Office source text and PO import suggestions through the authenticated cancellable client', async () => {
    await api.getHankIntakeSourcePreview(42, signal);
    await api.getHankIntakePurchaseOrderDraft(42, signal);
    expect(mockGet.mock.calls).toEqual([
      ['/hank/intake/files/42/source-preview', { signal }],
      ['/hank/intake/files/42/purchase-order-draft', { signal }],
    ]);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('encodes lot/serial values instead of treating their punctuation as a URL', async () => {
    await api.getHankTrace('lot', 'LOT/26 #A?heat=7', signal);
    expect(mockGet).toHaveBeenCalledWith('/hank/trace/lot/LOT%2F26%20%23A%3Fheat%3D7', { signal });
  });

  it('preserves the exact combined queue state and server partial-coverage flag', async () => {
    const queue = { checked_at: '2026-09-22T19:00:00Z', items: [], truncated: true };
    mockGet.mockResolvedValueOnce({ data: queue });
    expect(await api.getHankWorkQueue('waiting_on_other', signal)).toBe(queue);
    expect(mockGet).toHaveBeenCalledWith('/hank/work-queue', { params: { state: 'waiting_on_other' }, signal });
  });

  it('sends pagination and exact status/direction filters without translating them locally', async () => {
    const handoffs = { direction: 'received' as const, status: 'acknowledged' as const, limit: 20, before_id: 71 };
    const page = { limit: 20, before_id: 19 };
    await api.getHankHandoffs(handoffs, signal);
    await api.getHankRoutineRuns(page, signal);
    await api.getHankIntakes(page, signal);
    expect(mockGet.mock.calls).toEqual([
      ['/hank/handoffs', { params: handoffs, signal }],
      ['/hank/routine-runs', { params: page, signal }],
      ['/hank/intake', { params: page, signal }],
    ]);
  });

  it('keeps a batch recovery URL distinct from an exact-file queue link', async () => {
    await api.getHankIntake(42, signal);
    await api.getHankIntakeFile(42, signal);
    expect(mockGet.mock.calls).toEqual([
      ['/hank/intake/42', { signal }],
      ['/hank/intake/files/42', { signal }],
    ]);
  });

  it('reads one handoff, procedure, run and the same-company people search', async () => {
    await api.getHankHandoff(42, signal);
    await api.getHankRoutine(17, signal);
    await api.getHankRoutineRun(9, signal);
    await api.getHankRoutines(signal);
    await api.getHankHandoffPeople('A & B', signal);
    expect(mockGet.mock.calls).toEqual([
      ['/hank/handoffs/42', { signal }],
      ['/hank/routines/17', { signal }],
      ['/hank/routine-runs/9', { signal }],
      ['/hank/routines', { signal }],
      ['/hank/handoff-people', { params: { q: 'A & B' }, signal }],
    ]);
  });
});

describe('reviewed commands retain actor context and version', () => {
  it('saves the complete reviewed intake plan without rewriting nullable corrections', async () => {
    const plan: HankIntakePlan = {
      filing_mode: 'release_receipt_certificate',
      title: 'Reviewed heat certificate',
      document_type: 'material_cert',
      revision: 'A',
      part_id: 5,
      vendor_id: 6,
      purchase_order_id: 7,
      receipt_id: 8,
      reviewed_fields: [
        { name: 'heat_number', value: 'HEAT-2' },
        { name: 'lot_number', value: null },
      ],
      acknowledge_duplicate: true,
    };
    const payload = Object.freeze({ ...command, plan });
    expect(await api.planHankIntake(42, payload, signal)).toBe(serverResult);
    expect(mockPost).toHaveBeenCalledWith('/hank/intake/files/42/plan', payload, { signal });
    expect(mockPost.mock.calls[0][1]).toBe(payload);
  });

  it.each(['execute', 'retry', 'cancel'] as const)('sends intake %s with unchanged company/version', async action => {
    expect(await api.commandHankIntake(42, action, command, signal)).toBe(serverResult);
    expect(mockPost).toHaveBeenCalledWith(`/hank/intake/files/42/${action}`, command, { signal });
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('creates one explicitly addressed handoff with its stable request key', async () => {
    const payload: HankHandoffCreate = {
      expected_company_id: 17,
      request_key: requestKey,
      work_order_id: 6,
      recipient_id: 12,
      summary: 'Weld fixture ready',
      completed_work: 'Setup',
      remaining_work: 'Final pass',
      problems: 'Check clamp clearance',
      quantity_remaining: 3,
      document_ids: [4, 5],
    };
    expect(await api.createHankHandoff(payload, signal)).toBe(serverResult);
    expect(mockPost).toHaveBeenCalledWith('/hank/handoffs', payload, { signal });
  });

  it.each(['acknowledge', 'complete', 'cancel'] as const)(
    'sends handoff %s as an explicit versioned command',
    async action => {
      await api.commandHankHandoff(42, action, command, signal);
      expect(mockPost).toHaveBeenCalledWith(`/hank/handoffs/42/${action}`, command, { signal });
    }
  );

  const values: HankRoutineValues = {
    title: 'Review job setup',
    description: 'Before starting the run',
    steps: [{ kind: 'readiness', title: 'Readiness', instruction: 'Review current gaps.' }],
  };

  it('creates a routine with UUID identity and updates using PUT plus current version', async () => {
    const creation = { ...values, expected_company_id: 17, request_key: requestKey };
    const update = { ...values, ...command };
    expect(await api.createHankRoutine(creation, signal)).toBe(serverResult);
    expect(await api.updateHankRoutine(42, update, signal)).toBe(serverResult);
    expect(mockPost).toHaveBeenCalledWith('/hank/routines', creation, { signal });
    expect(mockPut).toHaveBeenCalledWith('/hank/routines/42', update, { signal });
  });

  it.each(['approve', 'archive'] as const)('retains version and company when requesting routine %s', async action => {
    await api.commandHankRoutine(42, action, command, signal);
    expect(mockPost).toHaveBeenCalledWith(`/hank/routines/42/${action}`, command, { signal });
  });

  it('starts a run with exact context and advances with the explicit completion evidence', async () => {
    const start = { ...command, request_key: requestKey, work_order_id: 6, purchase_order_id: 7 };
    const advance: HankRoutineAdvance = { ...command, note: 'Reviewed the saved receipt.', intake_file_id: 81 };
    await api.startHankRoutine(42, start, signal);
    await api.advanceHankRoutine(19, advance, signal);
    await api.cancelHankRoutineRun(19, command, signal);
    expect(mockPost.mock.calls).toEqual([
      ['/hank/routines/42/start', start, { signal }],
      ['/hank/routine-runs/19/advance', advance, { signal }],
      ['/hank/routine-runs/19/cancel', command, { signal }],
    ]);
  });

  it('propagates uncertain execution failure without silently resubmitting the write', async () => {
    const failure = new Error('Connection closed after submission');
    mockPost.mockRejectedValueOnce(failure);
    await expect(api.commandHankIntake(42, 'execute', command, signal)).rejects.toBe(failure);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });
});

describe('source bytes and multipart transport', () => {
  async function transportRecordedPost() {
    const axios = jest.requireActual<typeof import('axios')>('axios').default;
    const constructorOptions = mockCreate.mock.calls[0][0] as AxiosRequestConfig;
    let sent: InternalAxiosRequestConfig | undefined;
    const transport = axios.create({
      ...constructorOptions,
      adapter: async config => {
        sent = config;
        return { data: serverResult, status: 200, statusText: 'OK', headers: {}, config };
      },
    });
    const [url, body, config] = mockPost.mock.calls[0] as [string, FormData, AxiosRequestConfig];
    await transport.post(url, body, config);
    return sent;
  }

  it('preserves both PDF files and the request key through the real Axios request transform', async () => {
    const first = new File(['%PDF-first'], 'first.pdf', { type: 'application/pdf' });
    const second = new File(['%PDF-second'], 'second.pdf', { type: 'application/pdf' });
    const form = new FormData();
    form.append('expected_company_id', '17');
    form.append('request_key', requestKey);
    form.append('files', first);
    form.append('files', second);
    expect(await api.createHankIntake(form, signal)).toBe(serverResult);
    expect(mockPost.mock.calls[0][0]).toBe('/hank/intake');
    expect(mockPost.mock.calls[0][1]).toBe(form);
    const sent = await transportRecordedPost();
    expect(sent?.data).toBe(form);
    expect(sent?.signal).toBe(signal);
    expect((sent?.data as FormData).getAll('files')).toEqual([first, second]);
    expect((sent?.data as FormData).get('request_key')).toBe(requestKey);
  });

  it('preserves the photo, version and request identity as multipart rather than JSON', async () => {
    const photo = new File(['jpeg bytes'], 'fixture.jpg', { type: 'image/jpeg' });
    const form = new FormData();
    form.append('expected_company_id', '17');
    form.append('expected_version', '8');
    form.append('request_key', requestKey);
    form.append('file', photo);
    await api.attachHankHandoffPhoto(42, form, signal);
    expect(mockPost.mock.calls[0][0]).toBe('/hank/handoffs/42/attachments');
    const sent = await transportRecordedPost();
    expect(sent?.data).toBe(form);
    expect(sent?.signal).toBe(signal);
    expect((sent?.data as FormData).get('file')).toBe(photo);
    expect((sent?.data as FormData).get('expected_company_id')).toBe('17');
    expect((sent?.data as FormData).get('expected_version')).toBe('8');
  });

  it('downloads the PDF source and photo as authenticated Axios blobs', async () => {
    const source = new Blob(['%PDF-source'], { type: 'application/pdf' });
    const photo = new Blob(['photo'], { type: 'image/png' });
    mockGet.mockResolvedValueOnce({ data: source }).mockResolvedValueOnce({ data: photo });
    expect(await api.getHankIntakeSource(42, signal)).toBe(source);
    expect(await api.getHankHandoffPhoto(19, requestKey, signal)).toBe(photo);
    expect(mockGet.mock.calls).toEqual([
      ['/hank/intake/files/42/source', { signal, responseType: 'blob' }],
      [`/hank/handoffs/19/attachments/${requestKey}`, { signal, responseType: 'blob' }],
    ]);
  });

  it('passes cancellation through a source download without falling back to another request', async () => {
    const controller = new AbortController();
    const cancelled = new DOMException('Session changed', 'AbortError');
    mockGet.mockImplementationOnce(
      (_url, config: AxiosRequestConfig) =>
        new Promise((_resolve, reject) => {
          config.signal?.addEventListener?.('abort', () => reject(cancelled));
        })
    );
    const response = api.getHankIntakeSource(42, controller.signal);
    controller.abort();
    await expect(response).rejects.toBe(cancelled);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockPost).not.toHaveBeenCalled();
  });
});

it('loads receiving suggestions with the selected purchase order and cancellation signal', async () => {
  expect(await api.getHankIntakeReceivingDraft(51, 12, signal)).toBe(serverResult);
  expect(mockGet).toHaveBeenCalledWith('/hank/intake/files/51/receiving-draft', {
    params: { purchase_order_id: 12 },
    signal,
  });
  expect(mockPost).not.toHaveBeenCalled();
});
