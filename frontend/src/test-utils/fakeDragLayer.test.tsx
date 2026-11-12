/**
 * The fake drag layer and its loader stand-in, wired exactly as page suites wire them: the
 * mocked loader resolves to the fake, and `dragHarness` (imported normally) sees its props.
 */
import React from 'react';
import { render } from '@testing-library/react';
import { loadDashboardDragLayer, prefetchDashboardDragLayer } from '../components/dashboard/editor/loadDragLayer';
import { createDragBridge } from '../components/dashboard/editor/dragBridge';
import type { DashboardDragLayerProps } from '../components/dashboard/editor/dragLayerTypes';
import { FakeDragLayer, dragHarness } from './fakeDragLayer';

jest.mock('../components/dashboard/editor/loadDragLayer', () =>
  jest.requireActual('./fakeDragLayer').createFakeLoadDragLayerModule()
);

function layerProps(overrides: Partial<DashboardDragLayerProps> = {}): DashboardDragLayerProps {
  return {
    bridge: createDragBridge(),
    enabled: true,
    orders: { kpi: ['kpi.overdue'], panel: ['presence', 'capacity_overview'] },
    layoutKey: 'k1',
    reducedMotion: false,
    describe: id => ({ title: id, sizeLabel: 'Half' }),
    onDragStart: jest.fn(),
    onReorder: jest.fn(),
    onDragEnd: jest.fn(),
    onDragCancel: jest.fn(),
    ...overrides,
  };
}

describe('fakeDragLayer', () => {
  beforeEach(() => dragHarness.reset());

  it('stands in for the loader: resolves to the fake, and prefetch is a mock', async () => {
    await expect(loadDashboardDragLayer()).resolves.toBe(FakeDragLayer);
    prefetchDashboardDragLayer();
    expect(jest.mocked(prefetchDashboardDragLayer)).toHaveBeenCalledTimes(1);
    jest.mocked(loadDashboardDragLayer).mockRejectedValueOnce(new Error('Loading chunk failed'));
    await expect(loadDashboardDragLayer()).rejects.toThrow('Loading chunk failed');
    await expect(loadDashboardDragLayer()).resolves.toBe(FakeDragLayer);
  });

  it('captures the latest props and drives the callbacks', async () => {
    const Layer = await loadDashboardDragLayer();
    const props = layerProps();
    const { rerender, unmount, container } = render(<Layer {...props} />);
    // Like the real layer, it renders nothing in place.
    expect(container).toBeEmptyDOMElement();
    expect(dragHarness.mounted).toBe(true);
    expect(dragHarness.mounts).toBe(1);
    // JSX spreads props into a new object: same values, same callbacks.
    expect(dragHarness.latest()).toEqual(props);
    expect(dragHarness.latest().bridge).toBe(props.bridge);

    dragHarness.start('panel', 'presence');
    dragHarness.reorder('panel', ['capacity_overview', 'presence']);
    dragHarness.end('panel', 'presence');
    dragHarness.cancel('kpi', 'kpi.overdue');
    expect(props.onDragStart).toHaveBeenCalledWith('panel', 'presence');
    expect(props.onReorder).toHaveBeenCalledWith('panel', ['capacity_overview', 'presence']);
    expect(props.onDragEnd).toHaveBeenCalledWith('panel', 'presence');
    expect(props.onDragCancel).toHaveBeenCalledWith('kpi', 'kpi.overdue');

    const next = layerProps({ enabled: false, layoutKey: 'k2' });
    rerender(<Layer {...next} />);
    expect(dragHarness.latest().enabled).toBe(false);
    expect(dragHarness.mounts).toBe(1);

    unmount();
    expect(dragHarness.mounted).toBe(false);
    expect(() => dragHarness.latest()).toThrow('not mounted');
  });

  it('starts nothing while enabled is false, like a disabled dnd-kit draggable', async () => {
    const Layer = await loadDashboardDragLayer();
    const props = layerProps({ enabled: false });
    render(<Layer {...props} />);
    dragHarness.start('panel', 'presence');
    expect(props.onDragStart).not.toHaveBeenCalled();
  });
});
