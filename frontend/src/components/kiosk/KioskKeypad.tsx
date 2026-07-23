import React from 'react';
import { BackspaceIcon } from '@heroicons/react/24/outline';

interface KioskKeypadProps {
  value: string;
  onChange: (next: string) => void;
  maxLength?: number;
  disabled?: boolean;
  /** Test/automation id prefix for the keys. */
  idPrefix?: string;
  /**
   * Foundry key sizing (additive — existing consumers keep the default):
   * 'md' (default) ≈ the original 72px keys, 'lg' = 74px sign-in keys (1a),
   * 'sm' = 58px modal-numpad keys (1c/1d).
   */
  size?: 'sm' | 'md' | 'lg';
}

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'clear', '0', 'back'] as const;

const KEY_HEIGHT: Record<NonNullable<KioskKeypadProps['size']>, string> = {
  sm: 'h-[58px]',
  md: 'min-h-18',
  lg: 'h-[74px]',
};

const DIGIT_TEXT: Record<NonNullable<KioskKeypadProps['size']>, string> = {
  sm: 'text-[21px]',
  md: 'text-3xl',
  lg: 'text-2xl',
};

/**
 * Big-button numeric keypad (≥44px touch targets, no native spinners), in the
 * Foundry chrome: raised digit keys, sunken CLR/backspace, JetBrains Mono.
 * Digits only — kiosk quantities are whole parts and badge IDs are digits;
 * alphanumeric badge IDs still work through a physical wedge scanner.
 */
export default function KioskKeypad({
  value,
  onChange,
  maxLength = 6,
  disabled = false,
  idPrefix = 'kiosk-key',
  size = 'md',
}: KioskKeypadProps) {
  const press = (key: (typeof KEYS)[number]) => {
    if (disabled) return;
    if (key === 'clear') {
      onChange('');
      return;
    }
    if (key === 'back') {
      onChange(value.slice(0, -1));
      return;
    }
    if (value.length >= maxLength) return;
    // No leading zeros pile-up: "0" then "5" becomes "5".
    onChange(value === '0' ? key : value + key);
  };

  const gap = size === 'sm' ? 'gap-2' : 'gap-2.5';

  return (
    <div className={`grid grid-cols-3 ${gap}`} role="group" aria-label="Number pad">
      {KEYS.map((key) => (
        <button
          key={key}
          type="button"
          data-testid={`${idPrefix}-${key}`}
          aria-label={key === 'back' ? 'Backspace' : key === 'clear' ? 'Clear' : key}
          disabled={disabled}
          onClick={() => press(key)}
          className={`rounded-[4px] border font-mono transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 ${KEY_HEIGHT[size]} ${
            key === 'clear'
              ? 'border-fd-line bg-fd-sunken text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body'
              : key === 'back'
                ? 'border-fd-line bg-fd-sunken text-fd-body'
                : `border-fd-line bg-fd-raised font-semibold text-fd-ink ${DIGIT_TEXT[size]}`
          }`}
        >
          {key === 'back' ? (
            <BackspaceIcon className={`mx-auto ${size === 'sm' ? 'h-[22px] w-[22px]' : 'h-[26px] w-[26px]'}`} />
          ) : key === 'clear' ? (
            'Clear'
          ) : (
            key
          )}
        </button>
      ))}
    </div>
  );
}
