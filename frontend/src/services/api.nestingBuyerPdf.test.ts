const mockPost = jest.fn();
jest.mock('axios', () => ({
  __esModule: true,
  default: {
    create: () => ({
      post: mockPost,
      defaults: { headers: { common: {} } },
      interceptors: { request: { use: jest.fn() }, response: { use: jest.fn() } },
    }),
  },
}));
import api from './api';
import type { BuyerPdfReport } from '../features/nesting/lib/buyer-pdf-types';

const report: BuyerPdfReport = {
  version: 1,
  units: 'in',
  expectedCompanyId: 2,
  projectName: 'Synthetic café job',
  notes: 'Confirm reported material grade.',
  inputSha256: 'a'.repeat(64),
  solverVersion: 'werco-contour-v7',
  groups: [],
};
beforeEach(() => mockPost.mockReset());
function text(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

test('PDF transport pins the report company and exact JSON in bounded authenticated multipart', async () => {
  const signal = new AbortController().signal;
  const pdf = new Blob(['%PDF-1.7'], { type: 'application/pdf' });
  mockPost.mockResolvedValue({ data: pdf });
  expect(await api.generateNestingBuyerPdf(report, signal)).toBe(pdf);
  const [path, body, config] = mockPost.mock.calls[0];
  expect(path).toBe('/quote-nesting/buyer-pdf');
  expect(body).toBeInstanceOf(FormData);
  expect(Array.from(body.keys()).sort()).toEqual(['expected_company_id', 'report']);
  expect(body.get('expected_company_id')).toBe('2');
  const file = body.get('report');
  expect(file).toBeInstanceOf(Blob);
  expect(file.type).toBe('application/json');
  expect(await text(file)).toBe(JSON.stringify(report));
  expect(config).toEqual({
    signal,
    timeout: 120000,
    responseType: 'blob',
    headers: { 'Content-Type': 'multipart/form-data' },
  });
});

test.each(['The active company changed.', 'The PDF exceeds the page budget.'])(
  'JSON Blob rejection is readable: %s',
  async detail => {
    const data = new Blob([JSON.stringify({ detail })], { type: 'application/json' });
    Object.defineProperty(data, 'text', { value: () => text(data) });
    mockPost.mockRejectedValue({ response: { status: 409, data } });
    await expect(api.generateNestingBuyerPdf(report)).rejects.toThrow(detail);
    expect(mockPost).toHaveBeenCalledTimes(1);
  }
);

test('an unparseable Blob error stays an error, and a cancelled request is never retried', async () => {
  const data = new Blob(['<html>proxy failure</html>'], { type: 'text/html' });
  Object.defineProperty(data, 'text', { value: () => text(data) });
  mockPost.mockRejectedValueOnce({ response: { data } });
  await expect(api.generateNestingBuyerPdf(report)).rejects.toThrow('could not be generated');
  const controller = new AbortController();
  controller.abort();
  const error = new Error('cancelled');
  mockPost.mockRejectedValueOnce(error);
  await expect(api.generateNestingBuyerPdf(report, controller.signal)).rejects.toBe(error);
  expect(mockPost).toHaveBeenCalledTimes(2);
});
