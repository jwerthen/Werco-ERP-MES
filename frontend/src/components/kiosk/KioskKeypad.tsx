import React from 'react';
import { BackspaceIcon } from '@heroicons/react/24/solid';

interface KioskKeypadProps {
  value: string;
  onChange: (next: string) => void;
  maxLength?: number;
  disabled?: boolean;
  /** Test/automation id prefix for the keys. */
  idPrefix?: string;
}

const KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'clear', '0', 'back'] as const;

/**
 * Big-button numeric keypad (≥64px touch targets, no native spinners).
 * Digits only — kiosk quantities are whole parts and badge IDs are digits;
 * alphanumeric badge IDs still work through a physical wedge scanner.
 */
export default function KioskKeypad({ value, onChange, maxLength = 6, disabled = false, idPrefix = 'kiosk-key' }: KioskKeypadProps) {
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

  return (
    <div className="grid grid-cols-3 gap-2" role="group" aria-label="Number pad">
      {KEYS.map((key) => (
        <button
          key={key}
          type="button"
          data-testid={`${idPrefix}-${key}`}
          aria-label={key === 'back' ? 'Backspace' : key === 'clear' ? 'Clear' : key}
          disabled={disabled}
          onClick={() => press(key)}
          className={`min-h-18 rounded border text-3xl font-semibold transition-colors active:translate-y-px disabled:opacity-40 ${
            key === 'clear'
              ? 'border-fd-line bg-fd-sunken text-fd-mute text-lg uppercase tracking-widest'
              : key === 'back'
                ? 'border-fd-line bg-fd-sunken text-fd-body'
                : 'border-fd-line-bright bg-fd-raised text-fd-ink hover:bg-fd-panel'
          }`}
        >
          {key === 'back' ? <BackspaceIcon className="mx-auto h-8 w-8" /> : key === 'clear' ? 'Clear' : key}
        </button>
      ))}
    </div>
  );
}
