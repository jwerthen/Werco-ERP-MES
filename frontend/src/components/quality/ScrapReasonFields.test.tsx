/**
 * ScrapReasonFields — the shared desktop scrap-reason fragment (Lean Phase 1).
 *
 * Locks the two-mode contract every scrap-entry dialog relies on:
 *
 *  1. FALLBACK (zero active codes): the legacy REQUIRED SCRAP_REASONS picker,
 *     ariaLabel "Scrap reason", no detail field — exactly the pre-codes flow,
 *     so a company without codes (or a codes-fetch failure) is never bricked.
 *  2. CODES mode: a REQUIRED code select labeled "CODE — Name" plus an
 *     OPTIONAL free-text detail input.
 *
 * Plus the selection helpers the dialogs submit through: completeness
 * (code OR legacy reason), the payload mapping (code id + optional text vs
 * text-only), and the free-text projection used by the text-only
 * operation-complete endpoint.
 */
import React, { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  EMPTY_SCRAP_SELECTION,
  ScrapReasonFields,
  ScrapReasonSelection,
  isScrapSelectionComplete,
  scrapSelectionPayload,
  scrapSelectionText,
} from './ScrapReasonFields';
import type { ScrapReasonCode } from '../../types/scrapReason';

const CODES: ScrapReasonCode[] = [
  { id: 7, code: 'OT', name: 'Out of tolerance', category: 'operator', description: null, is_active: true, display_order: 1 },
  { id: 9, code: 'MAT', name: 'Material defect', category: 'material', description: null, is_active: true, display_order: 2 },
];

/** Controlled harness so selections persist across interactions. */
function Harness({ codes, onChangeSpy }: { codes: ScrapReasonCode[]; onChangeSpy?: (next: ScrapReasonSelection) => void }) {
  const [value, setValue] = useState<ScrapReasonSelection>(EMPTY_SCRAP_SELECTION);
  return (
    <ScrapReasonFields
      codes={codes}
      value={value}
      onChange={(next) => {
        onChangeSpy?.(next);
        setValue(next);
      }}
    />
  );
}

describe('ScrapReasonFields — fallback mode (no active codes)', () => {
  it('renders the legacy required picker and no detail field', () => {
    render(<Harness codes={[]} />);

    // Legacy SelectField carries the same accessible name as before.
    expect(screen.getByRole('button', { name: 'Scrap reason' })).toBeInTheDocument();
    expect(screen.getByText(/required when scrap is greater than zero/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/scrap detail/i)).not.toBeInTheDocument();

    // Legacy vocabulary, stored verbatim as text.
    fireEvent.click(screen.getByRole('button', { name: 'Scrap reason' }));
    fireEvent.mouseDown(screen.getByRole('option', { name: /Out of tolerance/i }));

    expect(screen.queryByText(/required when scrap is greater than zero/i)).not.toBeInTheDocument();
  });

  it('completeness + payload use the legacy reason text only', () => {
    expect(isScrapSelectionComplete([], EMPTY_SCRAP_SELECTION)).toBe(false);
    const chosen = { ...EMPTY_SCRAP_SELECTION, legacyReason: 'Out of tolerance' };
    expect(isScrapSelectionComplete([], chosen)).toBe(true);
    expect(scrapSelectionPayload([], chosen)).toEqual({ scrap_reason: 'Out of tolerance' });
    expect(scrapSelectionText([], chosen)).toBe('Out of tolerance');
  });
});

describe('ScrapReasonFields — codes mode', () => {
  it('renders CODE — Name options plus an optional detail field', () => {
    render(<Harness codes={CODES} />);

    fireEvent.click(screen.getByRole('button', { name: 'Scrap reason' }));
    expect(screen.getByRole('option', { name: /OT — Out of tolerance/ })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole('option', { name: /MAT — Material defect/ }));

    // Required error clears once a code is chosen; detail stays optional.
    expect(screen.queryByText(/required when scrap is greater than zero/i)).not.toBeInTheDocument();
    const detail = screen.getByLabelText(/scrap detail \(optional\)/i);
    fireEvent.change(detail, { target: { value: 'edge nick on 2 pcs' } });
    expect(detail).toHaveValue('edge nick on 2 pcs');
  });

  it('completeness requires a code; payload sends the id plus detail text when typed', () => {
    expect(isScrapSelectionComplete(CODES, { ...EMPTY_SCRAP_SELECTION, detail: 'text only' })).toBe(false);

    const codeOnly = { ...EMPTY_SCRAP_SELECTION, codeId: 7 };
    expect(isScrapSelectionComplete(CODES, codeOnly)).toBe(true);
    expect(scrapSelectionPayload(CODES, codeOnly)).toEqual({ scrap_reason_code_id: 7 });
    // Text-only endpoints get the "CODE — Name" label when no detail was typed.
    expect(scrapSelectionText(CODES, codeOnly)).toBe('OT — Out of tolerance');

    const codeAndDetail = { ...EMPTY_SCRAP_SELECTION, codeId: 9, detail: ' edge nick ' };
    expect(scrapSelectionPayload(CODES, codeAndDetail)).toEqual({
      scrap_reason_code_id: 9,
      scrap_reason: 'edge nick',
    });
    expect(scrapSelectionText(CODES, codeAndDetail)).toBe('edge nick');
  });
});
