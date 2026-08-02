/**
 * Materials — URL-param filters + value-only debounce.
 *
 * Locks the regression fix this page needed: the old code debounced the whole
 * loadMaterials CALLBACK (a 200ms setTimeout keyed on the callback identity),
 * so a TYPE-FILTER change ate the debounce delay and any dependency identity
 * change (e.g. showToast) could re-fire the load. Now only the search VALUE is
 * debounced (shared useDebouncedValue), and the load effect keys on
 * [debouncedSearch, typeFilter]:
 *
 *   1. TYPE FILTER FIRES IMMEDIATELY — changing the select refetches with NO
 *      timer advance (asserted under fake timers, where any debounce would
 *      hold the fetch forever).
 *   2. SEARCH IS STILL DEBOUNCED — typing alone fires nothing until 250ms idle.
 *   3. URL ROUND-TRIP — type/status filters read from and write to search
 *      params (`type`, `status`); defaults keep the URL clean, and Clear drops
 *      both params in one update.
 *
 * Materials uses the default no-op Toast context, so no ToastProvider is needed.
 */

import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../services/api';
import Materials from './Materials';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaterials: jest.fn(),
    createMaterial: jest.fn(),
    updateMaterial: jest.fn(),
    deleteMaterial: jest.fn(),
    importMaterialsCsv: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const materials = [
  {
    id: 1,
    part_number: 'RM-0001',
    name: 'Aluminum Sheet 5052',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    is_active: true,
    status: 'active',
    requires_inspection: true,
  },
  {
    id: 2,
    part_number: 'HW-0001',
    name: 'Rivet 1/8',
    part_type: 'hardware',
    unit_of_measure: 'each',
    is_active: true,
    status: 'obsolete',
    requires_inspection: false,
  },
];

/** Exposes the live URL search string so param round-trips are assertable. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Materials />
      <LocationProbe />
    </MemoryRouter>
  );
}

const locationSearch = () => screen.getByTestId('location-search').textContent;

async function waitForInitialLoad() {
  await waitFor(() => expect(mockedApi.getMaterials).toHaveBeenCalledTimes(1));
  // Let the resolved rows commit before the test switches to fake timers.
  await screen.findAllByText('RM-0001');
}

describe('Materials — type filter reloads immediately, only search is debounced', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMaterials.mockResolvedValue(materials as any);
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('refetches on a type-filter change with NO debounce delay (the regression)', async () => {
    renderAt('/materials');
    await waitForInitialLoad();
    expect(mockedApi.getMaterials).toHaveBeenLastCalledWith({});

    // Under fake timers a debounced reload could never fire without an explicit
    // timer advance — so a second call here proves the filter path is immediate.
    jest.useFakeTimers();
    fireEvent.change(screen.getByLabelText('Filter by type'), {
      target: { value: 'hardware' },
    });

    expect(mockedApi.getMaterials).toHaveBeenCalledTimes(2);
    expect(mockedApi.getMaterials).toHaveBeenLastCalledWith({ part_type: 'hardware' });
    // And the filter round-tripped into the URL.
    expect(locationSearch()).toBe('?type=hardware');

    // Flush the in-flight mock resolution so the test exits cleanly.
    await act(async () => {});
  });

  it('debounces the search value: no refetch until 250ms idle', async () => {
    renderAt('/materials');
    await waitForInitialLoad();

    jest.useFakeTimers();
    const input = screen.getByLabelText('Search materials') as HTMLInputElement;

    fireEvent.change(input, { target: { value: 'rivet' } });
    expect(input.value).toBe('rivet');
    // No fetch during the debounce window.
    act(() => {
      jest.advanceTimersByTime(249);
    });
    expect(mockedApi.getMaterials).toHaveBeenCalledTimes(1);

    // Crossing the boundary fires exactly one fetch with the trimmed query.
    await act(async () => {
      jest.advanceTimersByTime(1);
    });
    expect(mockedApi.getMaterials).toHaveBeenCalledTimes(2);
    expect(mockedApi.getMaterials).toHaveBeenLastCalledWith({ search: 'rivet' });
  });
});

describe('Materials — filters round-trip through URL params', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMaterials.mockResolvedValue(materials as any);
  });

  it('applies incoming ?type= and ?status= params to the selects and the fetch', async () => {
    renderAt('/materials?type=hardware&status=active');
    await waitFor(() => expect(mockedApi.getMaterials).toHaveBeenCalledTimes(1));

    expect(screen.getByLabelText('Filter by type')).toHaveValue('hardware');
    expect(screen.getByLabelText('Filter by status')).toHaveValue('active');
    // part_type is a server-side filter; status filters client-side.
    expect(mockedApi.getMaterials).toHaveBeenCalledWith({ part_type: 'hardware' });
  });

  it('Clear drops both params in a single update and returns to a clean URL', async () => {
    renderAt('/materials?type=hardware&status=active');
    await waitFor(() => expect(mockedApi.getMaterials).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: /^clear$/i }));

    expect(locationSearch()).toBe('');
    expect(screen.getByLabelText('Filter by type')).toHaveValue('');
    expect(screen.getByLabelText('Filter by status')).toHaveValue('');
    // The cleared type filter refetches without the param.
    await waitFor(() => expect(mockedApi.getMaterials).toHaveBeenLastCalledWith({}));
  });
});
