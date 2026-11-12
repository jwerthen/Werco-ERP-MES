/**
 * The jsdom drag/animation helpers do what their callers rely on: keyed rect stubs that
 * restore, mouse events with coordinates on the activator and on `document`, real
 * TouchEvents whose plain-object touches reach React and document listeners, and a
 * recording `animate` that uninstalls cleanly.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import {
  createTouchEvent,
  installFakeAnimate,
  mouseDrag,
  stubRects,
  toDomRect,
  touchCancel,
  touchEnd,
  touchMove,
  touchStart,
} from './dndDom';

describe('stubRects', () => {
  it('answers by key attribute or element, falls back to jsdom, and restores', () => {
    render(
      <div>
        <div data-widget-cell="presence">cell</div>
        <div data-testid="zone">zone</div>
        <div data-testid="unknown">unknown</div>
      </div>
    );
    const cell = screen.getByText('cell');
    const zone = screen.getByTestId('zone');
    const free = screen.getByTestId('unknown');
    const loose = document.createElement('section');

    const stub = stubRects({ presence: { left: 10, top: 20, width: 300, height: 120 } });
    stub.set('zone', { left: 0, top: 0, width: 1000, height: 400 });
    stub.setFor(loose, { left: 5, top: 6, width: 7, height: 8 });

    expect(cell.getBoundingClientRect()).toMatchObject({ left: 10, top: 20, right: 310, bottom: 140, width: 300 });
    expect(zone.getBoundingClientRect()).toMatchObject({ width: 1000, height: 400 });
    expect(loose.getBoundingClientRect()).toMatchObject({ left: 5, bottom: 14 });
    expect(free.getBoundingClientRect()).toMatchObject({ width: 0, height: 0 });

    stub.setAll(new Map([['presence', { left: 1, top: 2, width: 3, height: 4 }]]));
    expect(cell.getBoundingClientRect()).toMatchObject({ left: 1, width: 3 });
    expect(zone.getBoundingClientRect()).toMatchObject({ width: 0 });

    stub.restore();
    expect(cell.getBoundingClientRect()).toMatchObject({ left: 0, width: 0 });
  });

  it('builds DOMRect-shaped values', () => {
    expect(toDomRect({ left: 1, top: 2, width: 3, height: 4 })).toMatchObject({
      x: 1,
      y: 2,
      left: 1,
      top: 2,
      right: 4,
      bottom: 6,
    });
  });
});

describe('mouse helpers', () => {
  it('press on the target, then move and release on document, with coordinates', () => {
    const pressed = jest.fn<void, [number, number, number]>();
    render(
      <button type="button" onMouseDown={event => pressed(event.button, event.clientX, event.clientY)}>
        Move
      </button>
    );
    const moves: [number, number][] = [];
    const releases: [number, number][] = [];
    const onMove = (event: MouseEvent) => moves.push([event.clientX, event.clientY]);
    const onUp = (event: MouseEvent) => releases.push([event.clientX, event.clientY]);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    try {
      mouseDrag(screen.getByRole('button', { name: 'Move' }), [
        { x: 10, y: 10 },
        { x: 13, y: 10 },
        { x: 17, y: 10 },
      ]);
    } finally {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    expect(pressed).toHaveBeenCalledWith(0, 10, 10);
    expect(moves).toEqual([
      [13, 10],
      [17, 10],
    ]);
    expect(releases).toEqual([[17, 10]]);
    expect(() => mouseDrag(document.body, [])).toThrow();
  });
});

describe('touch helpers', () => {
  it('dispatch real TouchEvents with plain-object touches that bubble to React and document', () => {
    const starts: [number, number][] = [];
    render(
      <button
        type="button"
        onTouchStart={event => starts.push([event.touches[0].clientX, event.nativeEvent.touches[0].clientY])}
      >
        Move
      </button>
    );
    const button = screen.getByRole('button', { name: 'Move' });
    const seen: string[] = [];
    const onDocument = (event: Event) => seen.push(event.type);
    ['touchstart', 'touchmove', 'touchend'].forEach(type => document.addEventListener(type, onDocument));
    try {
      const start = touchStart(button, { x: 40, y: 50 });
      expect(start).toBeInstanceOf(TouchEvent);
      expect(start.cancelable).toBe(true);
      touchMove(button, { x: 44, y: 58 });
      const end = touchEnd(button, { x: 44, y: 58 });
      expect(end).toBeInstanceOf(TouchEvent);
    } finally {
      ['touchstart', 'touchmove', 'touchend'].forEach(type => document.removeEventListener(type, onDocument));
    }
    expect(starts).toEqual([[40, 50]]);
    expect(seen).toEqual(['touchstart', 'touchmove', 'touchend']);
  });

  it('empties `touches` on end while `changedTouches` keeps the point', () => {
    const target = document.createElement('div');
    const move = createTouchEvent('touchmove', target, { x: 1, y: 2 });
    const end = createTouchEvent('touchend', target, { x: 3, y: 4 });
    expect(move).toMatchObject({ touches: [{ clientX: 1, clientY: 2 }], changedTouches: [{ clientX: 1 }] });
    expect(end).toMatchObject({ touches: [], targetTouches: [], changedTouches: [{ clientX: 3, clientY: 4 }] });
  });
});

describe('installFakeAnimate', () => {
  it('records calls with their element and returns cancelable animations, then uninstalls', () => {
    expect(typeof Element.prototype.animate).toBe('undefined');
    const fake = installFakeAnimate();
    const node = document.createElement('div');
    const animation = node.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 10 });
    animation.cancel();
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0]).toMatchObject({
      element: node,
      keyframes: [{ opacity: 0 }, { opacity: 1 }],
      options: { duration: 10 },
    });
    expect(fake.calls[0].animation.cancel).toHaveBeenCalledTimes(1);
    expect(fake.animate).toHaveBeenCalledTimes(1);
    fake.clear();
    expect(fake.calls).toHaveLength(0);
    expect(fake.animate).not.toHaveBeenCalled();
    fake.restore();
    expect(typeof Element.prototype.animate).toBe('undefined');
  });

  it('nests: an inner install records alone, and its restore puts the outer one back', () => {
    const outer = installFakeAnimate();
    try {
      const inner = installFakeAnimate();
      const node = document.createElement('div');
      node.animate([{ opacity: 0 }], { duration: 1 });
      expect(inner.calls).toHaveLength(1);
      expect(outer.calls).toHaveLength(0);
      inner.restore();
      expect(Element.prototype.animate).toBe(outer.animate);
      node.animate([{ opacity: 1 }], { duration: 1 });
      expect(outer.calls).toHaveLength(1);
    } finally {
      outer.restore();
    }
    expect(typeof Element.prototype.animate).toBe('undefined');
  });
});

describe('stubRects key attributes', () => {
  it('reads only the attributes it is given, the first one present winning', () => {
    render(
      <div>
        <div data-zone="panel" data-testid="both">
          both
        </div>
        <div data-testid="testid-only">testid only</div>
      </div>
    );
    const rects = {
      panel: { left: 1, top: 2, width: 30, height: 40 },
      both: { left: 9, top: 9, width: 9, height: 9 },
      'testid-only': { left: 5, top: 5, width: 5, height: 5 },
    };
    const zoneOnly = stubRects(rects, { attributes: ['data-zone'] });
    try {
      // `data-zone` is the only key attribute: a data-testid key is never consulted.
      expect(screen.getByTestId('both').getBoundingClientRect()).toMatchObject({ left: 1, width: 30 });
      expect(screen.getByTestId('testid-only').getBoundingClientRect()).toMatchObject({ width: 0 });
    } finally {
      zoneOnly.restore();
    }
    const testIdFirst = stubRects(rects, { attributes: ['data-testid', 'data-zone'] });
    try {
      expect(screen.getByTestId('both').getBoundingClientRect()).toMatchObject({ left: 9, width: 9 });
      expect(screen.getByTestId('testid-only').getBoundingClientRect()).toMatchObject({ width: 5 });
    } finally {
      testIdFirst.restore();
    }
  });
});

describe('touchCancel', () => {
  it('dispatches a bubbling touchcancel whose point is only in changedTouches', () => {
    const target = document.createElement('div');
    document.body.appendChild(target);
    const seen: Event[] = [];
    const onCancel = (event: Event) => seen.push(event);
    document.addEventListener('touchcancel', onCancel);
    try {
      touchCancel(target, { x: 7, y: 8 });
    } finally {
      document.removeEventListener('touchcancel', onCancel);
      target.remove();
    }
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({ type: 'touchcancel', touches: [], changedTouches: [{ clientX: 7, clientY: 8 }] });
  });
});
