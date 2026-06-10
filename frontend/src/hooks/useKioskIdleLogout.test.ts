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

  it('clamps the warning window below a short timeout (?idle_logout_s=30 is not a permanent countdown)', () => {
    const onTimeout = jest.fn();
    // timeoutSeconds equals the default 30s warning — unclamped, every tick after
    // a reset would satisfy `remaining <= warning` and the banner would never hide.
    const { result } = renderHook(() => useKioskIdleLogout({ enabled: true, timeoutSeconds: 30, onTimeout }));

    // Desynchronize the deadline from the 1s tick grid: activity at t=500ms.
    act(() => {
      jest.advanceTimersByTime(500);
      window.dispatchEvent(new Event('pointerdown'));
    });

    // Tick at t=1000 (500ms after reset): 29.5s remain → ceil 30 > clamped 29 → quiet.
    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(result.current.countdownSeconds).toBeNull();

    // Tick at t=2000: 28.5s remain → ceil 29 ≤ 29 → countdown opens.
    act(() => {
      jest.advanceTimersByTime(1_000);
    });
    expect(result.current.countdownSeconds).toBe(29);

    // The full timeout still elapses from the reset before logout fires.
    act(() => {
      jest.advanceTimersByTime(28_000);
    });
    expect(onTimeout).not.toHaveBeenCalled();
    act(() => {
      jest.advanceTimersByTime(1_000);
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
