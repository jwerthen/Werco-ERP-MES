const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('axios', () => ({
  __esModule: true,
  default: {
    create: () => ({
      post: mockPost,
      get: mockGet,
      defaults: { headers: { common: {} } },
      interceptors: { request: { use: jest.fn() }, response: { use: jest.fn() } },
    }),
  },
}));

import api from './api';
import type { NestingDraftSave } from '../types/nestingDraft';

async function read(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}
beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

test.each([undefined, { draftId: 41, expectedVersion: 2 }])(
  'saves the exact company, key, snapshot and version for target %s',
  async target => {
    const request: NestingDraftSave = {
      companyId: 2,
      requestKey: '12345678-1234-4234-8234-123456789abc',
      estimateJson: '{"name":"<REF> synthetic"}',
      target,
    };
    const controller = new AbortController();
    const receipt = { draft_id: 41, revision_number: target ? 3 : 1, status: 'DRAFT' };
    mockPost.mockResolvedValue({ data: receipt });
    expect(await api.saveNestingDraft(request, controller.signal)).toBe(receipt);
    expect(await api.saveNestingDraft(request, controller.signal)).toBe(receipt);
    for (const [path, body, config] of mockPost.mock.calls) {
      expect(path).toBe(target ? '/quote-nesting/drafts/41/revisions' : '/quote-nesting/drafts');
      expect(body).toBeInstanceOf(FormData);
      expect(Array.from((body as FormData).keys()).sort()).toEqual(
        ['estimate', 'request_key', 'expected_company_id', ...(target ? ['expected_version'] : [])].sort()
      );
      expect(body.get('request_key')).toBe(request.requestKey);
      expect(body.get('expected_company_id')).toBe('2');
      expect(body.get('expected_version')).toBe(target ? '2' : null);
      const file = body.get('estimate');
      expect(file).toBeInstanceOf(Blob);
      expect(await read(file)).toBe(request.estimateJson);
      expect(config).toMatchObject({ signal: controller.signal, timeout: 120000 });
    }
  }
);
