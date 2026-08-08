/**
 * Quality — notification deep-link landings (`?tab=` and `?fai=`).
 *
 * The backend emits `/quality?tab=ncr`, `/quality?tab=car` and
 * `/quality?tab=fai&fai=<id>` (app/services/notification_links.py). Before this
 * change it emitted `/quality/ncr/<id>` and `/quality/fai/<id>`, neither of
 * which is a route — clicking a notification rendered the app's 404 screen.
 *
 * The load-bearing detail: both handlers are EFFECTS, not `useState` lazy
 * initializers. An initializer runs only at mount, so a bell click while the
 * user is already on /quality would change the query string and do nothing —
 * a silent no-op, which reads as success and is worse than a 404. The
 * "arriving while already mounted" cases below are the ones that pin that.
 *
 * NCR/CAR land record-LESS on purpose (the app has no detail view for either),
 * so there is no id to assert — only the tab. FAI is record-bearing and honest
 * because `openFaiDetail` does a real `GET /quality/fai/{id}`.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useSearchParams, useNavigate } from 'react-router-dom';
import QualityPage from './Quality';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const FAI_ROW = {
  id: 8,
  fai_number: 'FAI-000008',
  part_id: 1,
  part: { id: 1, part_number: 'PN-7731', name: 'Bracket, hinge' },
  part_revision: 'B',
  fai_type: 'full',
  status: 'in_progress',
  total_characteristics: 0,
  characteristics_passed: 0,
  characteristics_failed: 0,
  due_date: undefined,
  created_at: '2026-07-01T12:00:00Z',
};

const FAI_DETAIL = { ...FAI_ROW, work_order_id: 9, characteristics: [] };

/**
 * Surfaces the live query string so param consumption is assertable, and hands
 * the test a real in-router `navigate` — which is what a bell click does.
 * (Re-rendering a MemoryRouter with new `initialEntries` does NOT navigate: the
 * router reads them once and then owns its own stack.)
 */
let navigateTo: (to: string) => void = () => {
  throw new Error('router not mounted');
};

function RouterProbe() {
  const [params] = useSearchParams();
  navigateTo = useNavigate();
  return <div data-testid="query">{params.toString()}</div>;
}

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <ToastProvider>
        <RouterProbe />
        <Routes>
          <Route path="/quality" element={<QualityPage />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  );

const query = () => screen.getByTestId('query').textContent;

/**
 * The tab-strip button for a tab. The strip's labels are the bare acronyms
 * ('NCR' / 'CAR' / 'FAI'), and an exact-name match is required so it never
 * picks up the sibling "New NCR" action button.
 */
const tabButton = (label: string) => screen.getByRole('button', { name: label });

/** The strip marks the active tab with the brand underline. */
const isActive = (label: string) => tabButton(label).className.includes('border-werco-primary');

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getNCRs.mockResolvedValue([] as any);
  mockedApi.getCARs.mockResolvedValue([] as any);
  mockedApi.getFAIs.mockResolvedValue([FAI_ROW] as any);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 1 } as any);
  mockedApi.getParts.mockResolvedValue([] as any);
  mockedApi.getFAI.mockResolvedValue(FAI_DETAIL as any);
});

describe('?tab= landing', () => {
  test('?tab=car selects the CAR tab', async () => {
    renderAt('/quality?tab=car');
    await waitFor(() => expect(isActive('CAR')).toBe(true));
  });

  test('?tab=ncr selects the NCR tab (record-less — no id in the URL)', async () => {
    renderAt('/quality?tab=ncr');
    await waitFor(() => expect(isActive('NCR')).toBe(true));
    expect(query()).toBe('tab=ncr');
  });

  test('?tab=fai selects the FAI tab', async () => {
    renderAt('/quality?tab=fai');
    await waitFor(() => expect(isActive('FAI')).toBe(true));
  });

  test('an unrecognized tab value is ignored and the default tab stands', async () => {
    renderAt('/quality?tab=not-a-tab');
    await waitFor(() => expect(isActive('NCR')).toBe(true));
  });

  test('a manual tab click writes the tab to the URL and is not reverted by the effect', async () => {
    // The regression this guards: an effect that syncs FROM the URL will fight a
    // click that only sets local state, snapping the user back one render later.
    renderAt('/quality?tab=ncr');
    await waitFor(() => expect(isActive('NCR')).toBe(true));

    fireEvent.click(tabButton('CAR'));

    await waitFor(() => expect(query()).toContain('tab=car'));
    expect(isActive('CAR')).toBe(true);
    // Give the sync effect a chance to fight back; it must not.
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(isActive('CAR')).toBe(true);
  });
});

describe('?fai= landing', () => {
  test('?tab=fai&fai=8 fetches the report BY ID and opens the detail modal', async () => {
    renderAt('/quality?tab=fai&fai=8');
    await waitFor(() => expect(mockedApi.getFAI).toHaveBeenCalledWith(8));
    // The detail modal, not just the list row (both render the FAI number).
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('FAI-000008')).toBeInTheDocument();
  });

  test('the fai param is CONSUMED, while tab and filter survive', async () => {
    // Clearing is required: without it, closing the detail modal would leave the
    // param in place and the effect would immediately re-open it.
    renderAt('/quality?tab=fai&fai=8&filter=open');
    await waitFor(() => expect(mockedApi.getFAI).toHaveBeenCalledWith(8));

    await waitFor(() => expect(query()).not.toContain('fai=8'));
    expect(query()).toContain('tab=fai');
    expect(query()).toContain('filter=open');
  });

  test('a non-numeric fai id is dropped without any fetch', async () => {
    renderAt('/quality?tab=fai&fai=abc');
    await waitFor(() => expect(query()).not.toContain('fai=abc'));
    expect(mockedApi.getFAI).not.toHaveBeenCalled();
  });

  test('a failed by-id fetch surfaces an error rather than an empty modal', async () => {
    mockedApi.getFAI.mockRejectedValueOnce({ response: { data: { detail: 'FAI not found' } } });
    renderAt('/quality?tab=fai&fai=8');
    expect(await screen.findByText('FAI not found')).toBeInTheDocument();
  });
});

describe('arriving while already mounted (the lazy-initializer bug class)', () => {
  /**
   * Re-renders the page at a NEW url without unmounting, which is what an
   * in-app bell click does. A `useState(() => searchParams.get(...))`
   * initializer never observes this.
   */
  const renderThenNavigate = async (from: string, to: string) => {
    renderAt(from);
    await screen.findByRole('button', { name: 'NCR' });
    // Same router, same mounted page — only the query string changes.
    act(() => navigateTo(to));
  };

  test('a bell click to ?tab=car while on /quality switches the tab', async () => {
    await renderThenNavigate('/quality', '/quality?tab=car');
    await waitFor(() => expect(isActive('CAR')).toBe(true));
  });

  test('a bell click to ?fai= while on /quality opens the report', async () => {
    await renderThenNavigate('/quality', '/quality?tab=fai&fai=8');
    await waitFor(() => expect(mockedApi.getFAI).toHaveBeenCalledWith(8));
  });
});
