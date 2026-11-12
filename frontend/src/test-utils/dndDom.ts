/**
 * Driving drag-and-drop and layout animation in jsdom, which has no layout engine and no
 * Web Animations API.
 *
 * - `stubRects` — a scoped `getBoundingClientRect` stub keyed by a `data-*` attribute (or by
 *   element), mutable mid-test, with `restore()`. Unknown elements keep jsdom's zero rect.
 * - `mouseDown` / `mouseMove` / `mouseUp` / `mouseDrag` — dnd-kit's MouseSensor: the press
 *   on the activator, then moves and the release on `document` (where the sensor listens).
 *   RTL's `fireEvent` is already wrapped in `act`.
 * - `touchStart` / `touchMove` / `touchEnd` — dnd-kit's TouchSensor: real `TouchEvent`s
 *   carrying plain-object `touches` (jsdom has no `Touch` constructor), dispatched on the
 *   TARGET so they bubble to `document` (the sensor listens on the target), inside `act`.
 *   Pair with `jest.useFakeTimers()` for the long-press delay.
 * - `installFakeAnimate` — defines `Element.prototype.animate` and records every call.
 *
 * No `as any`: properties jsdom lacks are defined with `Object.defineProperty`.
 */
import { act, fireEvent } from '@testing-library/react';

export interface Point {
  x: number;
  y: number;
}

export interface RectInit {
  left: number;
  top: number;
  width: number;
  height: number;
}

export function toDomRect({ left, top, width, height }: RectInit): DOMRect {
  if (typeof DOMRect === 'function') return new DOMRect(left, top, width, height);
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({ x: left, y: top, left, top, width, height, right: left + width, bottom: top + height }),
  };
}

export type RectSource = Readonly<Record<string, RectInit>> | ReadonlyMap<string, RectInit>;

export interface RectStub {
  /** Set (or replace) the rect for a key. */
  set(key: string, rect: RectInit): void;
  /** Replace every keyed rect. */
  setAll(rects: RectSource): void;
  /** A rect for one element (zone containers, anything without a key attribute). */
  setFor(element: Element, rect: RectInit): void;
  /** Put the real `getBoundingClientRect` back. */
  restore(): void;
}

export interface StubRectsOptions {
  /** Attributes whose value is the key; the first one present on an element wins. */
  attributes?: readonly string[];
}

function isRectMap(rects: RectSource): rects is ReadonlyMap<string, RectInit> {
  return rects instanceof Map;
}

function entriesOf(rects: RectSource): [string, RectInit][] {
  if (isRectMap(rects)) return Array.from(rects.entries());
  return Object.keys(rects).map(key => [key, rects[key]]);
}

/**
 * Stub `getBoundingClientRect` for elements carrying a key attribute (default
 * `data-widget-cell`, then `data-testid`) whose value has a rect, and for elements given one
 * with `setFor`. Everything else keeps the original behavior. Call `restore()` (afterEach).
 */
export function stubRects(
  rects: RectSource = {},
  { attributes = ['data-widget-cell', 'data-testid'] }: StubRectsOptions = {}
): RectStub {
  const keyed = new Map<string, RectInit>(entriesOf(rects));
  const byElement = new WeakMap<Element, RectInit>();
  const original = Element.prototype.getBoundingClientRect;

  function stubbed(this: Element): DOMRect {
    const own = byElement.get(this);
    if (own) return toDomRect(own);
    for (const attribute of attributes) {
      const key = this.getAttribute(attribute);
      if (key !== null && keyed.has(key)) return toDomRect(keyed.get(key)!);
    }
    return original.call(this);
  }
  Object.defineProperty(Element.prototype, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value: stubbed,
  });

  return {
    set(key, rect) {
      keyed.set(key, rect);
    },
    setAll(next) {
      keyed.clear();
      entriesOf(next).forEach(([key, rect]) => keyed.set(key, rect));
    },
    setFor(element, rect) {
      byElement.set(element, rect);
    },
    restore() {
      Object.defineProperty(Element.prototype, 'getBoundingClientRect', {
        configurable: true,
        writable: true,
        value: original,
      });
    },
  };
}

// ── Mouse (dnd-kit MouseSensor) ──────────────────────────────────────────────

/** Primary-button press on the activator (the drag surface or the Move button). */
export function mouseDown(target: Element, at: Point): void {
  fireEvent.mouseDown(target, { button: 0, buttons: 1, clientX: at.x, clientY: at.y });
}

/** A move, on `document` by default (where the MouseSensor listens once pressed). */
export function mouseMove(at: Point, target: Document | Element = document): void {
  fireEvent.mouseMove(target, { button: 0, buttons: 1, clientX: at.x, clientY: at.y });
}

/** The release, on `document` by default. */
export function mouseUp(at: Point, target: Document | Element = document): void {
  fireEvent.mouseUp(target, { button: 0, buttons: 0, clientX: at.x, clientY: at.y });
}

/**
 * Press on `target` at `path[0]`, move through the rest of `path`, and (unless
 * `release: false`) release at the last point.
 */
export function mouseDrag(
  target: Element,
  path: readonly Point[],
  { release = true }: { release?: boolean } = {}
): void {
  if (path.length === 0) throw new Error('mouseDrag needs at least one point');
  mouseDown(target, path[0]);
  path.slice(1).forEach(point => mouseMove(point));
  if (release) mouseUp(path[path.length - 1]);
}

// ── Touch (dnd-kit TouchSensor) ──────────────────────────────────────────────

export type TouchEventType = 'touchstart' | 'touchmove' | 'touchend' | 'touchcancel';

/**
 * A bubbling, cancelable TouchEvent whose `touches` / `targetTouches` / `changedTouches`
 * are plain objects. While the finger is down (`start`/`move`) `touches` holds the point;
 * on `end`/`cancel` it is empty and the point is only in `changedTouches`.
 */
export function createTouchEvent(type: TouchEventType, target: Element, at: Point): Event {
  const event =
    typeof TouchEvent === 'function'
      ? new TouchEvent(type, { bubbles: true, cancelable: true })
      : new Event(type, { bubbles: true, cancelable: true });
  const touch = {
    identifier: 0,
    target,
    clientX: at.x,
    clientY: at.y,
    pageX: at.x,
    pageY: at.y,
    screenX: at.x,
    screenY: at.y,
  };
  const down = type === 'touchstart' || type === 'touchmove';
  Object.defineProperty(event, 'touches', { value: down ? [touch] : [] });
  Object.defineProperty(event, 'targetTouches', { value: down ? [touch] : [] });
  Object.defineProperty(event, 'changedTouches', { value: [touch] });
  return event;
}

/** Dispatch a touch event on `target` (it bubbles to `document`) inside `act`; returns the event. */
export function dispatchTouch(type: TouchEventType, target: Element, at: Point): Event {
  const event = createTouchEvent(type, target, at);
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

export const touchStart = (target: Element, at: Point) => dispatchTouch('touchstart', target, at);
export const touchMove = (target: Element, at: Point) => dispatchTouch('touchmove', target, at);
export const touchEnd = (target: Element, at: Point) => dispatchTouch('touchend', target, at);
export const touchCancel = (target: Element, at: Point) => dispatchTouch('touchcancel', target, at);

// ── Web Animations API (FLIP) ────────────────────────────────────────────────

export interface FakeAnimation {
  cancel: jest.Mock<void, []>;
}

export interface AnimateCall {
  element: Element;
  keyframes: Keyframe[] | PropertyIndexedKeyframes | null;
  options: number | KeyframeAnimationOptions | undefined;
  animation: FakeAnimation;
}

export interface FakeAnimate {
  /** Every `animate()` call, in order. */
  calls: AnimateCall[];
  animate: jest.Mock<
    FakeAnimation,
    [Keyframe[] | PropertyIndexedKeyframes | null, number | KeyframeAnimationOptions | undefined],
    Element
  >;
  /** Forget recorded calls (the stub stays installed). */
  clear(): void;
  /** Remove the stub (jsdom has no `animate`; one that existed before is put back). */
  restore(): void;
}

/** Define `Element.prototype.animate` as a recorder returning `{ cancel: jest.fn() }`. */
export function installFakeAnimate(): FakeAnimate {
  const existing = Object.getOwnPropertyDescriptor(Element.prototype, 'animate');
  const calls: AnimateCall[] = [];
  const animate = jest.fn(function (
    this: Element,
    keyframes: Keyframe[] | PropertyIndexedKeyframes | null,
    options: number | KeyframeAnimationOptions | undefined
  ): FakeAnimation {
    const animation: FakeAnimation = { cancel: jest.fn<void, []>() };
    calls.push({ element: this, keyframes, options, animation });
    return animation;
  });
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, writable: true, value: animate });
  return {
    calls,
    animate,
    clear() {
      calls.length = 0;
      animate.mockClear();
    },
    restore() {
      if (existing) Object.defineProperty(Element.prototype, 'animate', existing);
      else Reflect.deleteProperty(Element.prototype, 'animate');
    },
  };
}
