import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';
import type { KioskCrewQueueResponse, KioskStationSummary, KioskWorkCenterListResponse } from '../types/kioskStation';

jest.mock('../services/kioskStationClient', () => ({
  ...jest.requireActual('../services/kioskStationClient'),
  getStationToken: jest.fn(),
  getStoredStation: jest.fn(),
  setStoredStation: jest.fn(),
  clearStationToken: jest.fn(),
  getQueue: jest.fn(),
  getWorkCenters: jest.fn(),
  selectWorkCenter: jest.fn(),
  mintBadgeToken: jest.fn(),
  getMyActiveJob: jest.fn(),
}));

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;
const STATION: KioskStationSummary = {
  id: 3, label: 'Shop kiosk', work_center_id: 7,
  work_center_code: 'WELD1', work_center_name: 'Weld Bay 1',
};
const NEW_STATION: KioskStationSummary = {
  ...STATION, work_center_id: 8, work_center_code: 'LASER2', work_center_name: 'Laser Bay 2',
};
const WORK_CENTERS = {
  station: STATION,
  work_centers: [
    { id: 7, code: 'WELD1', name: 'Weld Bay 1' },
    { id: 8, code: 'LASER2', name: 'Laser Bay 2' },
  ],
};
const ITEM = {
  operation_id: 31, work_order_id: 9, work_order_number: 'WO-WELD-142',
  part_number: 'PN-7731', part_name: 'Frame', operation_number: '20', operation_name: 'Weld',
  work_center_id: 7, status: 'in_progress', quantity_ordered: 50,
  quantity_complete: 37, quantity_scrapped: 2, priority: 5, due_date: null, roster: [],
};
const QUEUE: KioskCrewQueueResponse = {
  queue: [ITEM], server_time: new Date().toISOString(), station: STATION,
};
const NEW_QUEUE: KioskCrewQueueResponse = {
  ...QUEUE, station: NEW_STATION,
  queue: [{ ...ITEM, operation_id: 32, work_order_number: 'WO-LASER-199', work_center_id: 8 }],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function renderKiosk() {
  return render(<MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}><CrewStationKiosk /></MemoryRouter>);
}

async function openPicker() {
  await screen.findByRole('button', { name: /WO-WELD-142/i });
  fireEvent.click(screen.getByRole('button', { name: 'Change workstation' }));
  await screen.findByRole('heading', { name: 'Choose workstation' });
}

function scanBadge(id: string) {
  for (const key of id) fireEvent.keyDown(window, { key });
  fireEvent.keyDown(window, { key: 'Enter' });
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue(QUEUE);
  mocked.getWorkCenters.mockResolvedValue(WORK_CENTERS);
  mocked.selectWorkCenter.mockResolvedValue(NEW_STATION);
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
});

afterEach(() => sessionStorage.clear());

describe('CrewStationKiosk workstation selection', () => {
  it('searches workstations and changes the board only after server confirmation', async () => {
    const assignment = deferred<KioskStationSummary>();
    const newQueue = deferred<KioskCrewQueueResponse>();
    mocked.selectWorkCenter.mockReturnValue(assignment.promise);
    mocked.getQueue.mockImplementation(id => id === 8 ? newQueue.promise : Promise.resolve(QUEUE));
    renderKiosk();
    await openPicker();

    expect(await screen.findByRole('button', { name: /WELD1.*Current/i })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a workstation' }), { target: { value: '  laser  ' } });
    expect(screen.queryByRole('button', { name: /WELD1/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /LASER2.*Laser Bay 2/i }));

    expect(mocked.selectWorkCenter).toHaveBeenCalledWith(8);
    expect(screen.getByRole('status')).toHaveTextContent('Changing workstation');
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /LASER2/ })).toBeDisabled();
    expect(mocked.getQueue).not.toHaveBeenCalledWith(8);

    // The assignment response is authoritative, including the displayed code.
    await act(async () => assignment.resolve({ ...NEW_STATION, work_center_code: 'LASER-CONFIRMED' }));
    expect(screen.getByText('Shop kiosk · LASER-CONFIRMED')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Choose workstation' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /WO-WELD-142/i })).not.toBeInTheDocument();
    expect(mocked.getQueue).toHaveBeenLastCalledWith(8);
    expect(screen.getByRole('button', { name: 'Change workstation' })).toBeEnabled();

    await act(async () => newQueue.resolve(NEW_QUEUE));
    expect(screen.getByRole('button', { name: /WO-LASER-199/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /WO-WELD-142/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Change workstation' })).toBeEnabled();
  });

  it('keeps a refused selection open and returns to the original workstation when cancelled', async () => {
    mocked.selectWorkCenter.mockRejectedValue(new KioskApiError(403, 'Workstation is unavailable', 'Workstation is unavailable'));
    renderKiosk();
    await openPicker();
    fireEvent.click(await screen.findByRole('button', { name: /LASER2/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Workstation is unavailable');
    expect(screen.getByRole('heading', { name: 'Choose workstation' })).toBeInTheDocument();
    expect(mocked.getQueue).not.toHaveBeenCalledWith(8);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByRole('button', { name: /WO-WELD-142/i })).toBeInTheDocument();
    expect(screen.getByText('Shop kiosk · WELD1')).toBeInTheDocument();
    expect(mocked.getQueue).toHaveBeenLastCalledWith(7);
    expect(mocked.clearStationToken).not.toHaveBeenCalled();
  });

  it('confirms the current workstation with the server before returning to its queue', async () => {
    mocked.selectWorkCenter.mockResolvedValue(STATION);
    renderKiosk();
    await openPicker();
    fireEvent.click(await screen.findByRole('button', { name: /WELD1.*Current/i }));
    expect(await screen.findByRole('button', { name: /WO-WELD-142/i })).toBeInTheDocument();
    expect(mocked.selectWorkCenter).toHaveBeenCalledWith(7);
    expect(mocked.getQueue).toHaveBeenLastCalledWith(7);
  });

  it('reconciles a lost assignment response before cancelling back to the confirmed workstation', async () => {
    mocked.selectWorkCenter.mockRejectedValue(new TypeError('Connection interrupted'));
    mocked.getWorkCenters.mockResolvedValueOnce(WORK_CENTERS)
      .mockResolvedValueOnce({ ...WORK_CENTERS, station: NEW_STATION });
    mocked.getQueue.mockImplementation(id => Promise.resolve(id === 8 ? NEW_QUEUE : QUEUE));
    renderKiosk();
    await openPicker();
    fireEvent.click(await screen.findByRole('button', { name: /LASER2/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Connection interrupted');
    await waitFor(() => expect(screen.getByRole('button', { name: /LASER2.*Current/ })).toHaveAttribute('aria-pressed', 'true'));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByRole('button', { name: /WO-LASER-199/ })).toBeInTheDocument();
    expect(screen.getByText('Shop kiosk · LASER2')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /WO-WELD-142/ })).not.toBeInTheDocument();
    expect(mocked.getQueue).toHaveBeenLastCalledWith(8);
  });

  it('can choose a workstation when the original queue cannot load', async () => {
    mocked.getQueue.mockRejectedValueOnce(new Error('Queue unavailable'));
    renderKiosk();
    await waitFor(() => expect(screen.getByTestId('kiosk-connection')).toHaveTextContent('Offline'));
    fireEvent.click(screen.getByRole('button', { name: 'Change workstation' }));
    mocked.getQueue.mockResolvedValue(NEW_QUEUE);
    fireEvent.click(await screen.findByRole('button', { name: /LASER2/ }));

    expect(await screen.findByRole('button', { name: /WO-LASER-199/ })).toBeInTheDocument();
    expect(mocked.selectWorkCenter).toHaveBeenCalledWith(8);
    expect(mocked.getQueue).toHaveBeenLastCalledWith(8);
  });

  it('caches only the current picker request and discards a cancelled list response', async () => {
    const oldList = deferred<KioskWorkCenterListResponse>();
    mocked.getWorkCenters.mockReturnValueOnce(oldList.promise)
      .mockResolvedValueOnce({ ...WORK_CENTERS, station: NEW_STATION });
    mocked.getQueue.mockImplementation(id => Promise.resolve(id === 8 ? NEW_QUEUE : QUEUE));
    renderKiosk();
    await openPicker();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await openPicker();
    expect(await screen.findByRole('button', { name: /LASER2.*Current/ })).toHaveAttribute('aria-pressed', 'true');
    expect(mocked.setStoredStation).toHaveBeenCalledTimes(1);
    expect(mocked.setStoredStation).toHaveBeenLastCalledWith(NEW_STATION);

    await act(async () => oldList.resolve(WORK_CENTERS));
    expect(screen.getByRole('button', { name: /LASER2.*Current/ })).toHaveAttribute('aria-pressed', 'true');
    expect(mocked.setStoredStation).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(await screen.findByRole('button', { name: /WO-LASER-199/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /WO-WELD-142/ })).not.toBeInTheDocument();
  });

  it.each(['list', 'selection'] as const)('locks the station when the %s request rejects its session', async stage => {
    const expired = new KioskApiError(401, 'Station revoked', 'Station revoked');
    if (stage === 'list') mocked.getWorkCenters.mockRejectedValue(expired);
    else mocked.selectWorkCenter.mockRejectedValue(expired);
    renderKiosk();
    await openPicker();
    if (stage === 'selection') fireEvent.click(await screen.findByRole('button', { name: /LASER2/ }));

    expect(await screen.findByText(/enter station pin/i)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Station session expired or revoked');
    expect(screen.queryByRole('heading', { name: 'Choose workstation' })).not.toBeInTheDocument();
    expect(mocked.clearStationToken).toHaveBeenCalled();
  });

  it('does not capture scanner input while the workstation picker owns the screen', async () => {
    mocked.mintBadgeToken.mockResolvedValue({ access_token: 'operator', user: { id: 11, full_name: 'Bob T', employee_id: 'E011' } });
    renderKiosk();
    await openPicker();
    await screen.findByRole('button', { name: /LASER2/ });
    scanBadge('E011');
    expect(mocked.mintBadgeToken).not.toHaveBeenCalled();
    expect(mocked.getMyActiveJob).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await screen.findByRole('button', { name: /WO-WELD-142/i });
    scanBadge('E011');
    await waitFor(() => expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(1));
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E011');
    await screen.findByRole('region', { name: 'Your jobs' });
  });

  it('prevents a workstation switch until an unconfirmed production report is resolved', async () => {
    const originalReport = {
      operatorId: 11, operationId: 31,
      body: { request_id: 'original-report-001', quantity_complete_delta: 3, source: 'kiosk' },
    };
    sessionStorage.setItem('kiosk_production_unconfirmed_crew', JSON.stringify(originalReport));
    renderKiosk();
    await screen.findByRole('button', { name: /WO-WELD-142/i });

    const change = screen.getByRole('button', { name: 'Change workstation' });
    expect(change).toBeDisabled();
    expect(change).toHaveAttribute('title', 'Resolve pending production before changing workstation.');
    fireEvent.click(change);
    expect(mocked.getWorkCenters).not.toHaveBeenCalled();
    expect(mocked.selectWorkCenter).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Check original report' })).toBeInTheDocument();
    expect(JSON.parse(sessionStorage.getItem('kiosk_production_unconfirmed_crew')!)).toEqual(originalReport);
  });

  it('keeps pending one-tap pieces attached to their workstation until they are resolved', async () => {
    const pending = {
      key: 'user:11|op:31', label: 'Bob T · WO-WELD-142 · Op 20 Weld', pieces: 2,
      identity: { operatorId: 11, operationId: 31 },
    };
    sessionStorage.setItem('kiosk_onetap_pending_crew', JSON.stringify(pending));
    renderKiosk();
    await screen.findByRole('button', { name: /WO-WELD-142/i });

    expect(screen.getByRole('button', { name: 'Change workstation' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Check original pieces' })).toBeInTheDocument();
    expect(mocked.getWorkCenters).not.toHaveBeenCalled();
    expect(mocked.selectWorkCenter).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem('kiosk_onetap_pending_crew')!)).toEqual(pending);
  });

  describe('in-flight queue responses', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      jest.clearAllTimers();
      jest.useRealTimers();
    });

    it.each(['old queue', 'old 401'] as const)('discards an %s response after switching workstations', async staleResult => {
      const oldPoll = deferred<KioskCrewQueueResponse>();
      renderKiosk();
      await screen.findByRole('button', { name: /WO-WELD-142/i });
      mocked.getQueue.mockReturnValueOnce(oldPoll.promise);
      await act(async () => { jest.advanceTimersByTime(10_000); });
      expect(mocked.getQueue).toHaveBeenCalledTimes(2);

      await openPicker();
      mocked.getQueue.mockResolvedValue(NEW_QUEUE);
      fireEvent.click(await screen.findByRole('button', { name: /LASER2/ }));
      await screen.findByRole('button', { name: /WO-LASER-199/i });

      await act(async () => {
        if (staleResult === 'old queue') oldPoll.resolve(QUEUE);
        else oldPoll.reject(new KioskApiError(401, 'Old queue session', 'Old queue session'));
      });
      expect(screen.getByText('Shop kiosk · LASER2')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /WO-LASER-199/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /WO-WELD-142/i })).not.toBeInTheDocument();
      expect(mocked.clearStationToken).not.toHaveBeenCalled();
    });
  });
});
