import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { NestingCatalogResponse } from '../../types/quoteNesting';
import { catalogFixture } from '../../test-utils/nestingCatalogFixtures';
import useNestingCatalog from './useNestingCatalog';

jest.mock('../../services/api', () => ({ __esModule: true, default: { getNestingMaterials: jest.fn() } }));
const getMaterials = jest.mocked(api.getNestingMaterials);
const page = (ids: number[], total = ids.length): NestingCatalogResponse => ({
  schema_version: 1,
  items: ids.map(catalogFixture),
  total,
  offset: 0,
  limit: 200,
});

describe('company-scoped catalog loading', () => {
  beforeEach(() => jest.clearAllMocks());

  it('aborts and ignores an earlier company response after company selection changes', async () => {
    let finishFirst!: (value: NestingCatalogResponse) => void;
    getMaterials
      .mockReturnValueOnce(
        new Promise(resolve => {
          finishFirst = resolve;
        })
      )
      .mockResolvedValueOnce(page([22]));
    const { result, rerender } = renderHook(({ companyId }) => useNestingCatalog(companyId), {
      initialProps: { companyId: 1 },
    });
    const signal = getMaterials.mock.calls[0][2];
    rerender({ companyId: 2 });
    await waitFor(() => expect(result.current.items.map(item => item.id)).toEqual([22]));
    expect(signal?.aborted).toBe(true);
    await act(async () => finishFirst(page([11])));
    expect(result.current.items.map(item => item.id)).toEqual([22]);
    expect(result.current.loading).toBe(false);
  });

  it('deduplicates overlapping pages and replaces snapshots on a refresh', async () => {
    getMaterials
      .mockResolvedValueOnce(page([11, 12], 3))
      .mockResolvedValueOnce(page([12, 13], 3))
      .mockResolvedValueOnce(page([14]));
    const { result } = renderHook(() => useNestingCatalog(2));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(result.current.complete).toBe(false);
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items.map(item => item.id)).toEqual([11, 12, 13]));
    expect(result.current.complete).toBe(true);
    expect(getMaterials).toHaveBeenNthCalledWith(2, 2, 200, expect.any(AbortSignal));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.items.map(item => item.id)).toEqual([14]));
    expect(result.current.total).toBe(1);
  });

  it('clears loading state when there is no longer an active company', async () => {
    getMaterials.mockReturnValueOnce(new Promise(() => undefined));
    const { result, rerender } = renderHook(({ companyId }: { companyId?: number }) => useNestingCatalog(companyId), {
      initialProps: { companyId: 2 } as { companyId?: number },
    });
    expect(result.current.loading).toBe(true);
    rerender({ companyId: undefined });
    expect(getMaterials.mock.calls[0][2]?.aborted).toBe(true);
    expect(result.current.items).toEqual([]);
    expect(result.current.total).toBe(0);
    expect(result.current.loading).toBe(false);
    expect(result.current.complete).toBe(false);
  });

  it('exposes denied reads and clears that company-specific error when company context is removed', async () => {
    getMaterials.mockRejectedValueOnce({ response: { data: { detail: 'Purchasing permission was revoked.' } } });
    const { result, rerender } = renderHook(({ companyId }: { companyId?: number }) => useNestingCatalog(companyId), {
      initialProps: { companyId: 2 } as { companyId?: number },
    });
    await waitFor(() => expect(result.current.error).toBe('Purchasing permission was revoked.'));
    expect(result.current.items).toEqual([]);
    expect(result.current.complete).toBe(false);
    rerender({ companyId: undefined });
    expect(result.current.error).toBe('');
    expect(result.current.loading).toBe(false);
    expect(result.current.complete).toBe(false);
  });
});
