/**
 * The bridge between the page's cells and the lazy drag layer: registration, stable ref
 * callbacks, snapshot identity for useSyncExternalStore, and `start` forwarding a pointer-down
 * to the published dnd-kit listener by event type (a no-op without one).
 */
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { createDragBridge, type DragBridge } from './dragBridge';

const element = (label: string) => {
  const node = document.createElement('div');
  node.dataset.label = label;
  return node;
};

/**
 * A Move button forwarding its real React events to `bridge.start` (a click stands in for
 * an event type the bridge does not forward). Returns what each `start` call answered.
 */
function renderForwarder(bridge: DragBridge, id: string) {
  const answers: boolean[] = [];
  render(
    React.createElement(
      'button',
      {
        type: 'button',
        onMouseDown: (event: React.MouseEvent<HTMLButtonElement>) => answers.push(bridge.start(id, event)),
        onTouchStart: (event: React.TouchEvent<HTMLButtonElement>) => answers.push(bridge.start(id, event)),
        onClick: (event: React.MouseEvent<HTMLButtonElement>) => answers.push(bridge.start(id, event)),
      },
      'Move'
    )
  );
  return { button: screen.getByRole('button', { name: 'Move' }), answers };
}

describe('createDragBridge', () => {
  it('registers and unregisters cells and zones, in a snapshot', () => {
    const bridge = createDragBridge();
    const a = element('a');
    const b = element('b');
    const strip = element('strip');
    expect(bridge.getSnapshot()).toEqual({ cells: [], zones: {} });

    bridge.setCell('kpi.overdue', 'kpi', a);
    bridge.setCell('presence', 'panel', b);
    bridge.setZone('kpi', strip);
    expect(bridge.getSnapshot()).toEqual({
      cells: [
        { id: 'kpi.overdue', zone: 'kpi', element: a },
        { id: 'presence', zone: 'panel', element: b },
      ],
      zones: { kpi: strip },
    });

    bridge.setCell('kpi.overdue', 'kpi', null);
    bridge.setZone('kpi', null);
    expect(bridge.getSnapshot()).toEqual({
      cells: [{ id: 'presence', zone: 'panel', element: b }],
      zones: {},
    });
  });

  it('keeps the same snapshot object until a registration really changes', () => {
    const bridge = createDragBridge();
    const a = element('a');
    bridge.setCell('presence', 'panel', a);
    const first = bridge.getSnapshot();
    expect(bridge.getSnapshot()).toBe(first);
    // Re-registering the same element is not a change.
    bridge.setCell('presence', 'panel', a);
    expect(bridge.getSnapshot()).toBe(first);
    // A detach + re-attach of the same element (a ref callback swap) ends where it started.
    bridge.setCell('presence', 'panel', null);
    bridge.setCell('presence', 'panel', a);
    expect(bridge.getSnapshot()).toBe(first);
    // A new element is a change.
    const replacement = element('a2');
    bridge.setCell('presence', 'panel', replacement);
    expect(bridge.getSnapshot()).not.toBe(first);
    expect(bridge.getSnapshot().cells[0].element).toBe(replacement);
  });

  it('hands out one stable ref callback per id and zone', () => {
    const bridge = createDragBridge();
    const ref = bridge.cellRef('presence', 'panel');
    expect(bridge.cellRef('presence', 'panel')).toBe(ref);
    expect(bridge.cellRef('presence', 'kpi')).not.toBe(ref);
    expect(bridge.zoneRef('panel')).toBe(bridge.zoneRef('panel'));

    const node = element('presence');
    ref(node);
    bridge.zoneRef('panel')(element('grid'));
    expect(bridge.getSnapshot().cells).toEqual([{ id: 'presence', zone: 'panel', element: node }]);
    expect(bridge.getSnapshot().zones.panel?.dataset.label).toBe('grid');
    ref(null);
    expect(bridge.getSnapshot().cells).toEqual([]);
  });

  it('notifies subscribers on registration changes only, and stops after unsubscribe', () => {
    const bridge = createDragBridge();
    const listener = jest.fn();
    const unsubscribe = bridge.subscribe(listener);
    const a = element('a');

    bridge.setCell('presence', 'panel', a);
    expect(listener).toHaveBeenCalledTimes(1);
    bridge.setCell('presence', 'panel', a);
    expect(listener).toHaveBeenCalledTimes(1);
    bridge.setZone('panel', element('grid'));
    expect(listener).toHaveBeenCalledTimes(2);
    bridge.setCell('missing', 'panel', null);
    bridge.setZone('kpi', null);
    expect(listener).toHaveBeenCalledTimes(2);
    // Activators are read at event time: publishing them is not a registration change.
    bridge.setActivators('presence', { onMouseDown: jest.fn() });
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    bridge.setCell('presence', 'panel', null);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it('start forwards a pointer-down to the matching listener by event type', () => {
    const bridge = createDragBridge();
    const onMouseDown = jest.fn<void, [React.MouseEvent]>();
    const onTouchStart = jest.fn<void, [React.TouchEvent]>();
    bridge.setActivators('presence', { onMouseDown, onTouchStart });
    const { button, answers } = renderForwarder(bridge, 'presence');

    fireEvent.mouseDown(button, { button: 0, clientX: 10, clientY: 20 });
    expect(answers).toEqual([true]);
    expect(onMouseDown).toHaveBeenCalledTimes(1);
    // dnd-kit's activator reads the native event: it arrives intact.
    const mouse = onMouseDown.mock.calls[0][0];
    expect(mouse.type).toBe('mousedown');
    expect(mouse.nativeEvent).toBeInstanceOf(MouseEvent);
    expect(mouse.nativeEvent.clientX).toBe(10);
    expect(onTouchStart).not.toHaveBeenCalled();

    fireEvent.touchStart(button);
    expect(answers).toEqual([true, true]);
    expect(onTouchStart).toHaveBeenCalledTimes(1);
    expect(onTouchStart.mock.calls[0][0].type).toBe('touchstart');
    expect(onMouseDown).toHaveBeenCalledTimes(1);
  });

  it('start is a no-op without activators, for another id, for another event type or after they are withdrawn', () => {
    const bridge = createDragBridge();
    const onMouseDown = jest.fn();
    const presence = renderForwarder(bridge, 'presence');
    fireEvent.mouseDown(presence.button);
    expect(presence.answers).toEqual([false]);

    // dnd-kit's SyntheticListenerMap shape; a non-function value is ignored.
    bridge.setActivators('presence', { onMouseDown, onTouchStart: 'not a function' });
    fireEvent.click(presence.button);
    fireEvent.touchStart(presence.button);
    expect(presence.answers).toEqual([false, false, false]);

    bridge.setActivators('presence', null);
    fireEvent.mouseDown(presence.button);
    expect(presence.answers).toEqual([false, false, false, false]);
    expect(onMouseDown).not.toHaveBeenCalled();
  });

  it('forwards only to the id it was asked for', () => {
    const bridge = createDragBridge();
    const onMouseDown = jest.fn();
    bridge.setActivators('presence', { onMouseDown });
    const other = renderForwarder(bridge, 'kpi.overdue');
    fireEvent.mouseDown(other.button);
    expect(other.answers).toEqual([false]);
    expect(onMouseDown).not.toHaveBeenCalled();
  });
});
