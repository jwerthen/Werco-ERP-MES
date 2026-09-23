import {
  getQueue, getStationToken, getStoredStation, getWorkCenters, KioskApiError,
  mintBadgeToken, selectWorkCenter, setStationToken, stationLogin,
} from './kioskStationClient';
import type { KioskStationSummary } from '../types/kioskStation';

const STATION_TOKEN = 'station-token-24h';
const STATION: KioskStationSummary = {
  id: 3, label: 'Shop kiosk', work_center_id: 7,
  work_center_code: 'WELD1', work_center_name: 'Weld Bay 1',
};
const NEW_STATION: KioskStationSummary = {
  ...STATION, work_center_id: 8, work_center_code: 'LASER2', work_center_name: 'Laser Bay 2',
};

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

describe('kioskStationClient workstation configuration', () => {
  let fetchMock: jest.Mock;
  const originalFetch = global.fetch;

  beforeEach(async () => {
    sessionStorage.clear();
    localStorage.clear();
    fetchMock = jest.fn();
    global.fetch = fetchMock as typeof fetch;
    fetchMock.mockResolvedValueOnce(jsonResponse({ access_token: STATION_TOKEN, station: STATION }));
    await stationLogin(3, '1234');
    fetchMock.mockReset();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    sessionStorage.clear();
    localStorage.clear();
  });

  it('lists workstations with the isolated station bearer even when an operator and user session exist', async () => {
    localStorage.setItem('access_token', 'normal-user-token');
    fetchMock.mockResolvedValueOnce(jsonResponse({ access_token: 'badge-operator-token', user: { id: 11 } }));
    await mintBadgeToken('E011');
    const list = { station: STATION, work_centers: [{ id: 8, code: 'LASER2', name: 'Laser Bay 2' }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(list));

    expect(await getWorkCenters()).toEqual(list);
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringMatching(/\/shop-floor\/kiosk-stations\/work-centers$/), {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${STATION_TOKEN}`,
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    expect(getStoredStation()).toEqual(STATION);
  });

  it('does not let an older list response overwrite a successfully selected workstation in storage', async () => {
    let finishList!: (value: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { finishList = resolve; }));
    const listing = getWorkCenters();
    fetchMock.mockResolvedValueOnce(jsonResponse(NEW_STATION));
    await selectWorkCenter(8);
    expect(getStoredStation()).toEqual(NEW_STATION);

    const staleList = { station: STATION, work_centers: [] };
    finishList(jsonResponse(staleList));
    expect(await listing).toEqual(staleList);
    expect(getStoredStation()).toEqual(NEW_STATION);
    expect(getStationToken()).toBe(STATION_TOKEN);
  });

  it('sends the chosen id with PUT and stores only the server-confirmed station after success', async () => {
    let finish!: (value: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { finish = resolve; }));
    const selection = selectWorkCenter(8);

    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/shop-floor\/kiosk-stations\/work-center$/), {
      method: 'PUT',
      headers: {
        Authorization: `Bearer ${STATION_TOKEN}`,
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({ work_center_id: 8 }),
    });
    expect(getStoredStation()).toEqual(STATION);
    const confirmed = { ...NEW_STATION, work_center_name: 'Server-confirmed laser bay' };
    finish(jsonResponse(confirmed));

    expect(await selection).toEqual(confirmed);
    expect(getStoredStation()).toEqual(confirmed);
    expect(getStationToken()).toBe(STATION_TOKEN);
  });

  it.each([400, 403, 404, 409])('keeps the previous station and token on a %s selection refusal', async status => {
    const detail = 'This workstation is unavailable';
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail }, status));

    await expect(selectWorkCenter(8)).rejects.toEqual(new KioskApiError(status, detail, detail));
    expect(getStoredStation()).toEqual(STATION);
    expect(getStationToken()).toBe(STATION_TOKEN);
  });

  it('keeps the previous station when the assignment request has a network failure', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Network unavailable'));
    await expect(selectWorkCenter(8)).rejects.toThrow('Network unavailable');
    expect(getStoredStation()).toEqual(STATION);
    expect(getStationToken()).toBe(STATION_TOKEN);
  });

  it.each(['list', 'selection'] as const)('clears only the station session on a 401 from %s', async endpoint => {
    localStorage.setItem('access_token', 'normal-user-token');
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Station expired' }, 401));

    const request = endpoint === 'list' ? getWorkCenters() : selectWorkCenter(8);
    await expect(request).rejects.toBeInstanceOf(KioskApiError);
    expect(getStationToken()).toBeNull();
    expect(getStoredStation()).toBeNull();
    expect(localStorage.getItem('access_token')).toBe('normal-user-token');
  });

  it.each(['queue', 'list'] as const)('keeps a newer station session when an old %s request returns 401', async endpoint => {
    let finish!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(resolve => { finish = resolve; }));
    const oldRequest = endpoint === 'queue' ? getQueue(7) : getWorkCenters();
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(`Bearer ${STATION_TOKEN}`);

    setStationToken('new-session');
    finish(jsonResponse({ detail: 'Old station session expired' }, 401));
    await expect(oldRequest).rejects.toBeInstanceOf(KioskApiError);
    expect(getStationToken()).toBe('new-session');
    expect(getStoredStation()).toEqual(STATION);
  });
});
