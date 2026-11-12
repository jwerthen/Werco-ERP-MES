/**
 * The lazy drag-layer loader: one shared import, a rejected import clears the memo so the
 * next call imports again, and prefetch swallows its failure.
 */
import type { ComponentType } from 'react';
import type { DashboardDragLayerProps } from './dragLayerTypes';

type Loader = typeof import('./loadDragLayer');

const Layer: ComponentType<DashboardDragLayerProps> = () => null;

/** A fresh loader module whose `./DashboardDragLayer` import fails while `state.fail` is true. */
function freshLoader(state: { fail: boolean; imports: number }): Loader {
  jest.resetModules();
  jest.doMock('./DashboardDragLayer', () => {
    state.imports += 1;
    if (state.fail) throw new Error('Loading chunk failed');
    return { __esModule: true, default: Layer };
  });
  const loader: Loader = require('./loadDragLayer');
  return loader;
}

describe('loadDashboardDragLayer', () => {
  afterEach(() => {
    jest.dontMock('./DashboardDragLayer');
  });

  it('resolves the default export and shares one import between callers', async () => {
    const state = { fail: false, imports: 0 };
    const loader = freshLoader(state);
    const first = loader.loadDashboardDragLayer();
    expect(loader.loadDashboardDragLayer()).toBe(first);
    await expect(first).resolves.toBe(Layer);
    expect(loader.loadDashboardDragLayer()).toBe(first);
    expect(state.imports).toBe(1);
  });

  it('clears the memo when the import rejects, so the next call imports again', async () => {
    const state = { fail: true, imports: 0 };
    const loader = freshLoader(state);
    const failed = loader.loadDashboardDragLayer();
    await expect(failed).rejects.toThrow('Loading chunk failed');

    state.fail = false;
    const retried = loader.loadDashboardDragLayer();
    expect(retried).not.toBe(failed);
    await expect(retried).resolves.toBe(Layer);
    expect(state.imports).toBe(2);
  });

  it('prefetch warms the shared import and swallows a failure', async () => {
    const state = { fail: true, imports: 0 };
    const loader = freshLoader(state);
    expect(() => loader.prefetchDashboardDragLayer()).not.toThrow();
    // Let the failed prefetch settle (no unhandled rejection), then a real load retries.
    await new Promise(resolve => setTimeout(resolve, 0));
    state.fail = false;
    await expect(loader.loadDashboardDragLayer()).resolves.toBe(Layer);
    expect(state.imports).toBe(2);

    loader.prefetchDashboardDragLayer();
    expect(state.imports).toBe(2);
  });
});
