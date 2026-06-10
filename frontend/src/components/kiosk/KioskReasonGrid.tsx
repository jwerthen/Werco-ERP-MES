import React from 'react';
import { KioskReason } from './kioskConstants';

interface KioskReasonGridProps {
  reasons: KioskReason[];
  selected: string | null;
  onSelect: (value: string) => void;
  disabled?: boolean;
  /** Accent for the selected state: red for scrap, amber for hold. */
  tone?: 'red' | 'amber';
}

/**
 * Button grid for choosing a reason. There is intentionally NO default and no
 * free-text alternative — the operator must make an explicit choice.
 */
export default function KioskReasonGrid({ reasons, selected, onSelect, disabled = false, tone = 'red' }: KioskReasonGridProps) {
  const selectedClasses =
    tone === 'red' ? 'border-fd-red bg-fd-red/15 text-fd-red' : 'border-fd-amber bg-fd-amber/15 text-fd-amber';

  return (
    <div className="grid grid-cols-2 gap-2" role="group" aria-label="Reason">
      {reasons.map((reason) => {
        const isSelected = selected === reason.value;
        return (
          <button
            key={reason.value}
            type="button"
            disabled={disabled}
            aria-pressed={isSelected}
            onClick={() => onSelect(reason.value)}
            className={`min-h-18 rounded border px-3 text-lg font-semibold leading-tight transition-colors active:translate-y-px disabled:opacity-40 ${
              isSelected ? selectedClasses : 'border-fd-line bg-fd-raised text-fd-body hover:border-fd-line-bright'
            }`}
          >
            {reason.label}
          </button>
        );
      })}
    </div>
  );
}
