import { renderHook, act } from '@testing-library/react';
import { useKioskIdleLogout } from './useKioskIdleLogout';

describe('useKioskIdleLogout', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('shows a 30s countdown before logging out, then fires onTimeout exactly once', () => {
    const onTimeout = jest.fn();
    const { result } = renderHook(() =>
      useKioskIdleLogout({ enabled: true, timeoutSeconds: 40, onTimeout })
    );

    expect(result.current.countdownSeconds).toBeNull();

    // 10s idle of a 40s timeout → 30s remaining → countdown window opens.
    act(() => {
      jest.advanceTimersByTime(10_000);
    });
    expect(result.current.countdownSeconds).toBe(30);

    act(() => {
      jest.advanceTimersByTime(5_000);
    });
    expect(result.current.countdownSeconds).toBe(25);
    expect(onTimeout).not.toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(25_000);
    });
    expect(onTimeout).toHaveBeenCalledTimes(1);

    // No double-fire after the deadline.
    act(() => {
      jest.advanceTimersByTime(10_000);
    });
    expect(onTimeout).toHaveBeenCalledTimes(1);
  });

  it('resets the countdown on any activity (tap to stay logged in)', () => {
    const onTimeout = jest.fn();
    const { result } = renderHook(() =>
      useKioskIdleLogout({ enabled: true, timeoutSeconds: 40, onTimeout })
    );

    act(() => {
      jest.advanceTimersByTime(15_000);
    });
    expect(result.current.countdownSeconds).toBe(25);

    act(() => {
      window.dispatchEvent(new Event('pointerdown'));
    });
    expect(result.current.countdownSeconds).toBeNull();

    // Full timeout must elapse again from the activity.
    act(() => {
      jest.advanceTimersByTime(39_000);
    });
    expect(onTimeout).not.toHaveBeenCalled();
    act(() => {
      jest.advanceTimersByTime(2_000);
    });
    expect(onTimeout).toHaveBeenCalledTimes(1);
  });

  it('stays disarmed while disabled (badge screen)', () => {
    const onTimeout = jest.fn();
    const { result } = renderHook(() =>
      useKioskIdleLogout({ enabled: false, timeoutSeconds: 40, onTimeout })
    );

    act(() => {
      jest.advanceTimersByTime(120_000);
    });
    expect(result.current.countdownSeconds).toBeNull();
    expect(onTimeout).not.toHaveBeenCalled();
  });
});
