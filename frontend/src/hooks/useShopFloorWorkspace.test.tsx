import { act, renderHook } from '@testing-library/react';
import { useShopFloorWorkspace } from './useShopFloorWorkspace';

const alice = { id: 3, company_id: 1 };
const bob = { id: 4, company_id: 1 };
const key = 'shop_floor_workspace:v1:company:1:user:3';

beforeEach(() => sessionStorage.clear());
afterEach(() => jest.restoreAllMocks());

it('restores the exact operation and time entry after an idle sign-out and same-user sign-in', () => {
  const first = renderHook(() => useShopFloorWorkspace(alice));
  act(() =>
    first.result.current.updateWorkspace({
      selectedOperationId: 82,
      activeTimeEntryId: 90,
      view: 'my-work',
      workCenterId: 4,
    })
  );
  first.unmount();
  const second = renderHook(() => useShopFloorWorkspace(alice));
  expect(second.result.current.workspace).toMatchObject({
    selectedOperationId: 82,
    activeTimeEntryId: 90,
    view: 'my-work',
    workCenterId: 4,
  });
});

it('never exposes or writes another operator or company workspace when identity changes', () => {
  const { result, rerender } = renderHook(({ user }) => useShopFloorWorkspace(user), { initialProps: { user: alice } });
  act(() => result.current.updateWorkspace({ selectedOperationId: 82, activeTimeEntryId: 90 }));
  rerender({ user: bob });
  expect(result.current.workspace.selectedOperationId).toBeNull();
  act(() => result.current.updateWorkspace({ selectedOperationId: 22 }));
  rerender({ user: { id: 3, company_id: 2 } });
  expect(result.current.workspace.selectedOperationId).toBeNull();
  rerender({ user: alice });
  expect(result.current.workspace.selectedOperationId).toBe(82);
});

it.each([null, { id: 3 }, { id: 3, company_id: 0 }])(
  'does not persist a workspace for an incomplete identity %j',
  user => {
    const { result } = renderHook(() => useShopFloorWorkspace(user));
    act(() => result.current.updateWorkspace({ selectedOperationId: 82 }));
    expect(result.current.workspace.savedAt).toBeNull();
    expect(sessionStorage.length).toBe(0);
  }
);

it('expires saved job context after a day and rejects invalid values', () => {
  sessionStorage.setItem(
    key,
    JSON.stringify({
      selectedOperationId: 82,
      activeTimeEntryId: 90,
      view: 'my-work',
      workCenterId: null,
      savedAt: Date.now() - 25 * 60 * 60_000,
    })
  );
  const expired = renderHook(() => useShopFloorWorkspace(alice));
  expect(expired.result.current.workspace.savedAt).toBeNull();
  expired.unmount();
  sessionStorage.setItem(key, '{broken');
  const invalid = renderHook(() => useShopFloorWorkspace(alice));
  expect(invalid.result.current.workspace.savedAt).toBeNull();
});

it('clears only the signed-in operator workspace', () => {
  sessionStorage.setItem('unrelated', 'keep');
  const { result } = renderHook(() => useShopFloorWorkspace(alice));
  act(() => result.current.updateWorkspace({ selectedOperationId: 82 }));
  act(() => result.current.clearWorkspace());
  expect(result.current.workspace.selectedOperationId).toBeNull();
  expect(sessionStorage.getItem(key)).toBeNull();
  expect(sessionStorage.getItem('unrelated')).toBe('keep');
});

it('keeps the screen usable when browser storage is unavailable', () => {
  jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new Error('Unavailable');
  });
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('Unavailable');
  });
  const { result } = renderHook(() => useShopFloorWorkspace(alice));
  act(() => result.current.updateWorkspace({ selectedOperationId: 82 }));
  expect(result.current.workspace.selectedOperationId).toBe(82);
});
