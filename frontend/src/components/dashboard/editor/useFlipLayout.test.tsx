/**
 * useFlipLayout with stubbed rects and a recording `Element.prototype.animate`: only cells
 * that moved glide (including one whose index is unchanged but whose row moved), new cells
 * fade in, a previous animation is cancelled, and nothing plays under reduced motion,
 * without `animate`, without a snapshot or from a stale one.
 */
import React, { useLayoutEffect, useRef } from 'react';
import { render } from '@testing-library/react';
import {
  installFakeAnimate,
  stubRects,
  type FakeAnimate,
  type RectInit,
  type RectStub,
} from '../../../test-utils/dndDom';
import {
  FLIP_EASING,
  FLIP_ENTER_MS,
  FLIP_MOVE_MS,
  FLIP_SNAPSHOT_MAX_AGE_MS,
  useFlipLayout,
  type FlipLayout,
} from './useFlipLayout';

let flip: FlipLayout | null = null;

function FlipHarness({ ids, disabled = false }: { ids: string[]; disabled?: boolean }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const layout = useFlipLayout(rootRef, { disabled });
  useLayoutEffect(() => {
    flip = layout;
  }, [layout]);
  return (
    <div ref={rootRef}>
      {ids.map(id => (
        <div key={id} data-widget-cell={id}>
          {id}
        </div>
      ))}
      {/* Not a widget cell: never measured or animated. */}
      <div data-testid="other">other</div>
    </div>
  );
}

const box = (left: number, top: number, width = 100, height = 80): RectInit => ({ left, top, width, height });

function capture() {
  if (!flip) throw new Error('FlipHarness is not mounted');
  flip.capture();
}

const animatedIds = (fake: FakeAnimate) => fake.calls.map(call => call.element.getAttribute('data-widget-cell'));

describe('useFlipLayout', () => {
  let rects: RectStub;
  let fake: FakeAnimate;

  beforeEach(() => {
    flip = null;
    rects = stubRects();
    fake = installFakeAnimate();
  });

  afterEach(() => {
    rects.restore();
    fake.restore();
    jest.restoreAllMocks();
  });

  it('glides only the cells that moved, including an index-stable cell whose row moved', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0), c: box(0, 96), d: box(500, 500) });
    const { rerender } = render(<FlipHarness ids={['a', 'b', 'c', 'd']} />);
    expect(fake.calls).toHaveLength(0);

    capture();
    // b and a swap; c keeps index 2 but its row moved down; d did not move.
    rects.setAll({ b: box(0, 0), a: box(116, 0), c: box(0, 140), d: box(500, 500) });
    rerender(<FlipHarness ids={['b', 'a', 'c', 'd']} />);

    expect(animatedIds(fake)).toEqual(['b', 'a', 'c']);
    const byId = new Map(fake.calls.map(call => [call.element.getAttribute('data-widget-cell'), call] as const));
    expect(byId.get('b')!.keyframes).toEqual([{ transform: 'translate(116px, 0px)' }, { transform: 'none' }]);
    expect(byId.get('a')!.keyframes).toEqual([{ transform: 'translate(-116px, 0px)' }, { transform: 'none' }]);
    expect(byId.get('c')!.keyframes).toEqual([{ transform: 'translate(0px, -44px)' }, { transform: 'none' }]);
    expect(byId.get('c')!.options).toEqual({ duration: FLIP_MOVE_MS, easing: FLIP_EASING });
    expect(FLIP_MOVE_MS).toBe(200);
    expect(FLIP_EASING).toBe('cubic-bezier(0.2,0,0,1)');
  });

  it('fades and scales in a cell that was not there', () => {
    rects.setAll({ a: box(0, 0) });
    const { rerender } = render(<FlipHarness ids={['a']} />);
    capture();
    rects.setAll({ a: box(0, 0), n: box(116, 0) });
    rerender(<FlipHarness ids={['a', 'n']} />);
    expect(animatedIds(fake)).toEqual(['n']);
    expect(fake.calls[0].keyframes).toEqual([
      { opacity: 0, transform: 'scale(0.98)' },
      { opacity: 1, transform: 'none' },
    ]);
    expect(fake.calls[0].options).toEqual({ duration: FLIP_ENTER_MS, easing: FLIP_EASING });
    expect(FLIP_ENTER_MS).toBe(180);
  });

  it('fades in the first cell added to an empty root', () => {
    const { rerender } = render(<FlipHarness ids={[]} />);
    capture();
    rects.setAll({ n: box(0, 0) });
    rerender(<FlipHarness ids={['n']} />);
    expect(animatedIds(fake)).toEqual(['n']);
    expect(fake.calls[0].options).toEqual({ duration: FLIP_ENTER_MS, easing: FLIP_EASING });
  });

  it('plays once per capture, and cancels the previous animation before the next one', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    capture();
    rects.setAll({ b: box(0, 0), a: box(116, 0) });
    rerender(<FlipHarness ids={['b', 'a']} />);
    expect(fake.calls).toHaveLength(2);
    const [first, second] = fake.calls.map(call => call.animation);

    // A render without a new capture (a data refresh) animates nothing.
    rerender(<FlipHarness ids={['b', 'a']} />);
    expect(fake.calls).toHaveLength(2);

    capture();
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    rerender(<FlipHarness ids={['a', 'b']} />);
    expect(first.cancel).toHaveBeenCalledTimes(1);
    expect(second.cancel).toHaveBeenCalledTimes(1);
    expect(fake.calls).toHaveLength(4);
  });

  it('does nothing without a snapshot', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    rects.setAll({ b: box(0, 0), a: box(116, 0) });
    rerender(<FlipHarness ids={['b', 'a']} />);
    expect(fake.calls).toHaveLength(0);
  });

  it('does nothing under reduced motion', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} disabled />);
    capture();
    rects.setAll({ b: box(0, 0), a: box(116, 0), n: box(232, 0) });
    rerender(<FlipHarness ids={['b', 'a', 'n']} disabled />);
    expect(fake.calls).toHaveLength(0);
  });

  it('respects reduced motion switched on in the very commit that plays a captured change', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    capture();
    rects.setAll({ b: box(0, 0), a: box(116, 0) });
    rerender(<FlipHarness ids={['b', 'a']} disabled />);
    expect(fake.calls).toHaveLength(0);
  });

  it('leaves sub-pixel layout noise alone', () => {
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    capture();
    rects.setAll({ a: box(0.3, 0.4), b: box(116, 40) });
    rerender(<FlipHarness ids={['a', 'b']} />);
    expect(animatedIds(fake)).toEqual(['b']);
  });

  it('does nothing where Element.prototype.animate is missing (jsdom)', () => {
    fake.restore();
    expect(typeof Element.prototype.animate).toBe('undefined');
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    capture();
    rects.setAll({ b: box(0, 0), a: box(116, 0) });
    expect(() => rerender(<FlipHarness ids={['b', 'a']} />)).not.toThrow();
    // No snapshot was kept either: installing animate later plays nothing for that change.
    fake = installFakeAnimate();
    rerender(<FlipHarness ids={['b', 'a']} />);
    expect(fake.calls).toHaveLength(0);
  });

  it('discards a snapshot whose change never rendered in time', () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    rects.setAll({ a: box(0, 0), b: box(116, 0) });
    const { rerender } = render(<FlipHarness ids={['a', 'b']} />);
    capture();
    now.mockReturnValue(1_000_000 + FLIP_SNAPSHOT_MAX_AGE_MS + 1);
    rects.setAll({ b: box(0, 0), a: box(116, 0) });
    rerender(<FlipHarness ids={['b', 'a']} />);
    expect(fake.calls).toHaveLength(0);
  });
});
