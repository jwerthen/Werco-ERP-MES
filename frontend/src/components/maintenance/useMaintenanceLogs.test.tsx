/**
 * The log reader behind the work-order and history dialogs: a key's read uses that key's loader,
 * a new key starts from an empty list, only the latest answer lands, and a reload handed out for an
 * earlier key does nothing once the key has moved on.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { useMaintenanceLogs } from './useMaintenanceLogs';
import type { MaintenanceLogEntry } from '../../types/maintenance';

const entry = (id: number, description: string): MaintenanceLogEntry => ({
  id,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  maintenance_wo_id: id,
  event_type: 'note',
  description,
  performed_by: 7,
  performed_by_name: 'Pat Synthetic',
  event_date: '2026-11-19T14:30:00Z',
  cost: 0,
  created_at: '2026-11-19T14:30:00Z',
});

type Props = { logKey: string | null; load: () => Promise<MaintenanceLogEntry[]> };

const renderLogs = (initial: Props) =>
  renderHook(({ logKey, load }: Props) => useMaintenanceLogs(logKey, load), { initialProps: initial });

describe('useMaintenanceLogs', () => {
  it('ignores a reload for a key that is no longer current', async () => {
    const loadA = jest.fn(async () => [entry(1, 'Work order A')]);
    const loadB = jest.fn(async () => [entry(2, 'Work order B')]);
    const { result, rerender } = renderLogs({ logKey: 'work-order:1', load: loadA });
    await waitFor(() => expect(result.current.logs.map(log => log.description)).toEqual(['Work order A']));
    const reloadA = result.current.reload;

    rerender({ logKey: 'work-order:2', load: loadB });
    await waitFor(() => expect(result.current.logs.map(log => log.description)).toEqual(['Work order B']));
    expect(loadA).toHaveBeenCalledTimes(1);
    expect(loadB).toHaveBeenCalledTimes(1);

    // An add on work order A finishing after the dialog moved to B: its reload is stale.
    act(() => reloadA());
    expect(result.current.loading).toBe(false);
    expect(loadA).toHaveBeenCalledTimes(1);
    expect(loadB).toHaveBeenCalledTimes(1);
    expect(result.current.logs.map(log => log.description)).toEqual(['Work order B']);

    // The current key's reload still re-reads with its own loader.
    act(() => result.current.reload());
    await waitFor(() => expect(loadB).toHaveBeenCalledTimes(2));
    expect(loadA).toHaveBeenCalledTimes(1);
  });

  it('drops a slow answer for an earlier key, and starts a new key from an empty list', async () => {
    let answerA: (rows: MaintenanceLogEntry[]) => void = () => undefined;
    const loadA = jest.fn(
      () =>
        new Promise<MaintenanceLogEntry[]>(resolve => {
          answerA = resolve;
        })
    );
    const loadB = jest.fn(
      () =>
        new Promise<MaintenanceLogEntry[]>(() => {
          // never answers: the list must stay empty, not show A's rows
        })
    );
    const { result, rerender } = renderLogs({ logKey: 'asset:5', load: loadA });
    await waitFor(() => expect(loadA).toHaveBeenCalledTimes(1));
    rerender({ logKey: 'work_center:3', load: loadB });
    await waitFor(() => expect(loadB).toHaveBeenCalledTimes(1));

    await act(async () => answerA([entry(1, 'Compressor')]));
    expect(result.current.logs).toEqual([]);
    expect(result.current.loading).toBe(true);
  });

  it('reads nothing while closed (null key)', () => {
    const load = jest.fn(async () => [entry(1, 'x')]);
    const { result } = renderLogs({ logKey: null, load });
    expect(load).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ logs: [], loading: false, error: null });
  });
});
