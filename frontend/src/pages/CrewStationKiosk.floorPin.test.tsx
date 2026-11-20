/**
 * CrewStationKiosk — the floor PIN on a crew-station badge scan (AS9100D 8.5.2).
 *
 * Every crew-station mint runs through `signOnWithBadge`: a badge with a PIN answers 401
 * `floor_pin_required`, the KioskFloorPinOverlay opens over the station for THAT scan, and
 * the mint is resent with badge + PIN. Pinned here, at the board's badge-first scan:
 *  - the PIN is resent with the same badge, and the minted operator token is what the
 *    next read uses;
 *  - a wrong PIN keeps the pad with the refusal;
 *  - Cancel / Escape return to the scan with nothing shown and nothing re-sent;
 *  - the IDLE FLOW RESET (90 s, never a station lock) clears a walked-away pad, the scan
 *    resolves as cancelled (no error, no second mint), and the NEXT badge is challenged
 *    normally -- a reset must not leave a stale "cancelled" flag that swallows it;
 *  - `floor_pin_not_set` is shown as its message, not as a raw object.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';

jest.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: jest.fn(() => ({ promise: Promise.reject(new Error('no pdf.js in jest')) })),
}));
jest.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ __esModule: true, default: 'mock-worker-url' }), {
  virtual: true,
});

// Keep KioskApiError REAL (instanceof checks in the page) but mock every call.
jest.mock('../services/kioskStationClient', () => {
  const actual = jest.requireActual('../services/kioskStationClient');
  return {
    __esModule: true,
    ...actual,
    getStationToken: jest.fn(),
    setStationToken: jest.fn(),
    clearStationToken: jest.fn(),
    getStoredStation: jest.fn(),
    stationLogin: jest.fn(),
    getQueue: jest.fn(),
    mintBadgeToken: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportProduction: jest.fn(),
    completeOperation: jest.fn(),
    holdOperation: jest.fn(),
    getOperationSteps: jest.fn(),
    getOperationDocuments: jest.fn(),
    fetchDocumentBlob: jest.fn(),
  };
});

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;

const STATION = {
  id: 3,
  label: 'Weld Bay Kiosk',
  work_center_id: 7,
  work_center_code: 'WELD1',
  work_center_name: 'Weld Bay 1',
};

const ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket',
  operation_number: '10',
  operation_name: 'Weld out',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  roster: [],
  steps_total: 0,
  steps_recorded: 0,
};

const QUEUE_RES = { queue: [ITEM], server_time: new Date().toISOString(), station: STATION };
const ALICE = { id: 13, full_name: 'Alice W', employee_id: 'E013' };
const ALICE_MINT = { access_token: 'op-token-alice', user: ALICE };

const challenge = () =>
  new KioskApiError(401, { code: 'floor_pin_required', message: 'Enter your PIN.' }, 'Enter your PIN.');
const wrongPin = () => new KioskApiError(401, 'Invalid badge or PIN', 'Invalid badge or PIN');
const NOT_SET_MESSAGE = 'No PIN is set for this badge. Ask a supervisor to set one.';
const notSet = () => new KioskApiError(403, { code: 'floor_pin_not_set', message: NOT_SET_MESSAGE }, NOT_SET_MESSAGE);

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

/** Type a badge on the window (wedge scanner) and hit Enter. */
function scanBadge(id: string) {
  id.split('').forEach(key => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

function typePin(pin: string) {
  pin.split('').forEach(k => fireEvent.click(screen.getByTestId(`floor-pin-key-${k}`)));
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

async function boardReady() {
  await screen.findByRole('button', { name: /WO-2026-0142/i });
}

const padIsOpen = () => screen.queryByTestId('kiosk-floor-pin-dialog') !== null;

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
  mocked.getQueue.mockResolvedValue(QUEUE_RES as never);
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] } as never);
});

describe('CrewStationKiosk floor PIN at the badge-first scan', () => {
  it('opens the pad over the station and resends the same badge with the PIN', async () => {
    mocked.mintBadgeToken.mockRejectedValueOnce(challenge()).mockResolvedValueOnce(ALICE_MINT as never);
    renderKiosk();
    await boardReady();

    scanBadge('E013');
    const dialog = await screen.findByRole('dialog', { name: 'Enter your PIN' });
    expect(dialog).toHaveAttribute('data-testid', 'kiosk-floor-pin-dialog');
    expect(mocked.mintBadgeToken.mock.calls[0]).toEqual(['E013']);

    typePin('0912');

    expect(await screen.findByRole('heading', { name: 'Alice W' })).toBeInTheDocument();
    expect(mocked.mintBadgeToken.mock.calls[1]).toEqual(['E013', '0912']);
    expect(mocked.getMyActiveJob).toHaveBeenCalledWith('op-token-alice');
    expect(padIsOpen()).toBe(false);
  });

  it('a wrong PIN keeps the pad up with the refusal', async () => {
    mocked.mintBadgeToken
      .mockRejectedValueOnce(challenge())
      .mockRejectedValueOnce(wrongPin())
      .mockResolvedValueOnce(ALICE_MINT as never);
    renderKiosk();
    await boardReady();

    scanBadge('E013');
    await screen.findByTestId('kiosk-floor-pin-dialog');
    typePin('1357');

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid badge or PIN');
    expect(padIsOpen()).toBe(true);
    typePin('0912');
    expect(await screen.findByRole('heading', { name: 'Alice W' })).toBeInTheDocument();
    expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(3);
  });

  it('Cancel returns to the scan with nothing shown and nothing re-sent', async () => {
    mocked.mintBadgeToken.mockRejectedValueOnce(challenge());
    renderKiosk();
    await boardReady();

    scanBadge('E013');
    await screen.findByTestId('kiosk-floor-pin-dialog');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(padIsOpen()).toBe(false));
    expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Enter your PIN|Invalid badge|Could not read that badge/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /WO-2026-0142/i })).toBeInTheDocument();
  });

  it('Escape is Cancel', async () => {
    mocked.mintBadgeToken.mockRejectedValueOnce(challenge());
    renderKiosk();
    await boardReady();

    scanBadge('E013');
    await screen.findByTestId('kiosk-floor-pin-dialog');
    fireEvent.keyDown(screen.getByTestId('floor-pin-input'), { key: 'Escape' });

    await waitFor(() => expect(padIsOpen()).toBe(false));
    expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(1);
  });

  it('floor_pin_not_set shows its message', async () => {
    mocked.mintBadgeToken.mockRejectedValueOnce(notSet());
    renderKiosk();
    await boardReady();

    scanBadge('E013');

    expect(await screen.findByText(NOT_SET_MESSAGE)).toBeInTheDocument();
    expect(padIsOpen()).toBe(false);
    expect(screen.queryByText(/floor_pin_not_set/)).not.toBeInTheDocument();
  });

  describe('idle flow reset', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('clears a walked-away pad without an error or a second mint, and the next badge is challenged again', async () => {
      mocked.mintBadgeToken
        .mockRejectedValueOnce(challenge())
        .mockRejectedValueOnce(challenge())
        .mockResolvedValueOnce(ALICE_MINT as never);
      renderKiosk();
      await boardReady();

      scanBadge('E013');
      await screen.findByTestId('kiosk-floor-pin-dialog');

      // Just short of the 90 s reset the pad is still waiting.
      await act(async () => {
        jest.advanceTimersByTime(85_000);
      });
      expect(padIsOpen()).toBe(true);

      await act(async () => {
        jest.advanceTimersByTime(10_000);
      });

      expect(padIsOpen()).toBe(false);
      expect(mocked.mintBadgeToken).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/Invalid badge|Could not read that badge/)).not.toBeInTheDocument();
      expect(mocked.clearStationToken).not.toHaveBeenCalled();
      expect(screen.getByRole('button', { name: /WO-2026-0142/i })).toBeInTheDocument();

      // The reset cancelled THAT scan only: the next badge draws the pad again and signs in.
      scanBadge('E013');
      await screen.findByTestId('kiosk-floor-pin-dialog');
      typePin('0912');
      expect(await screen.findByRole('heading', { name: 'Alice W' })).toBeInTheDocument();
      expect(mocked.mintBadgeToken.mock.calls).toEqual([['E013'], ['E013'], ['E013', '0912']]);
    });

    it('a touch on the pad keeps it from resetting', async () => {
      mocked.mintBadgeToken.mockRejectedValueOnce(challenge());
      renderKiosk();
      await boardReady();

      scanBadge('E013');
      await screen.findByTestId('kiosk-floor-pin-dialog');
      await act(async () => {
        jest.advanceTimersByTime(80_000);
      });
      fireEvent.pointerDown(screen.getByTestId('floor-pin-key-5'));
      await act(async () => {
        jest.advanceTimersByTime(80_000);
      });

      expect(padIsOpen()).toBe(true);
    });
  });
});
