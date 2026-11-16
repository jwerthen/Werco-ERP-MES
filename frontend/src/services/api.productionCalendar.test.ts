/**
 * Production-calendar client boundary.
 *
 * `getProductionCalendar` feeds the header HUD on every page. A body that is not a
 * calendar (a stub's `{}`, an HTML page from a proxy, another API version) must reject,
 * so each caller's existing failure path runs, instead of resolving into a render that
 * throws on a missing field and takes the whole app down.
 *
 * axios is mocked at the module boundary (same pattern as api.spc.test.ts).
 */

const mockGet = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: jest.fn(),
  put: jest.fn(),
  patch: jest.fn(),
  delete: jest.fn(),
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
import { wercoCalendar } from '../test-utils/productionCalendarFixtures';

const ok = (data: unknown) => ({ status: 200, data, headers: {} });

afterEach(() => jest.clearAllMocks());

test('a well-formed calendar is returned as sent', async () => {
  const calendar = wercoCalendar();
  mockGet.mockResolvedValueOnce(ok(calendar));
  await expect(api.getProductionCalendar()).resolves.toEqual(calendar);
  expect(mockGet).toHaveBeenCalledWith('/production-calendar');
});

test.each([
  ['an empty object', {}],
  ['an HTML page', '<!doctype html><html></html>'],
  ['a calendar without today', { ...wercoCalendar(), today: undefined }],
])('a 200 carrying %s rejects instead of resolving', async (_label, body) => {
  mockGet.mockResolvedValueOnce(ok(body));
  await expect(api.getProductionCalendar()).rejects.toThrow(
    'The production calendar response was not in the expected format.'
  );
});
