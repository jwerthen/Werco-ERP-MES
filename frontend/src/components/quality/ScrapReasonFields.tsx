/**
 * ScrapReasonFields — the shared desktop scrap-reason capture fragment
 * (Lean Phase 1 / issue #88). Render it whenever a dialog's scrap quantity is
 * greater than zero.
 *
 * Two modes, decided by whether the company has ACTIVE scrap reason codes:
 *
 *  - CODES mode: a REQUIRED code select (options "CODE — Name", display_order)
 *    plus an OPTIONAL free-text detail field. The submit payload carries
 *    scrap_reason_code_id and, when detail was typed, scrap_reason.
 *  - FALLBACK mode (zero active codes): the legacy REQUIRED picker over the
 *    hardcoded SCRAP_REASONS list, stored verbatim in scrap_reason — exactly
 *    the pre-codes behavior, so a company that has not set up codes (or a
 *    codes-fetch failure) never has its scrap flow bricked.
 *
 * The selection state, validity rule, and payload mapping live here so every
 * surface (shop floor dialogs, WO complete modal) enforces the same
 * scrap-requires-a-reason invariant the backend does (422 otherwise).
 */

import React from 'react';
import { FormField } from '../ui/FormField';
import { SelectField } from '../ui/SelectField';
import { SCRAP_REASONS } from '../kiosk/kioskConstants';
import { ScrapReasonCode, scrapCodeLabel } from '../../types/scrapReason';

/** One state shape covers both modes; only the mode-relevant parts are read. */
export interface ScrapReasonSelection {
  /** Selected structured code id (codes mode). */
  codeId: number | null;
  /** Optional free-text detail accompanying a code (codes mode). */
  detail: string;
  /** Selected legacy SCRAP_REASONS value (fallback mode). */
  legacyReason: string;
}

export const EMPTY_SCRAP_SELECTION: ScrapReasonSelection = {
  codeId: null,
  detail: '',
  legacyReason: '',
};

/** The required piece is chosen: a code (codes mode) or a legacy reason. */
export function isScrapSelectionComplete(codes: ScrapReasonCode[], selection: ScrapReasonSelection): boolean {
  return codes.length > 0 ? selection.codeId != null : Boolean(selection.legacyReason);
}

/**
 * Map the selection to the shop-floor payload fields. Only call for scrap > 0.
 * Codes mode sends the id (+ detail text when typed); fallback sends the
 * legacy reason text only — matching the backend's code-OR-text rule.
 */
export function scrapSelectionPayload(
  codes: ScrapReasonCode[],
  selection: ScrapReasonSelection
): { scrap_reason?: string; scrap_reason_code_id?: number } {
  if (codes.length > 0) {
    const detail = selection.detail.trim();
    return {
      ...(selection.codeId != null ? { scrap_reason_code_id: selection.codeId } : {}),
      ...(detail ? { scrap_reason: detail } : {}),
    };
  }
  return selection.legacyReason ? { scrap_reason: selection.legacyReason } : {};
}

/**
 * Human-readable reason text for endpoints that only take free text (the
 * office operation-complete path): typed detail wins, else the chosen code's
 * "CODE — Name" label, else the legacy reason.
 */
export function scrapSelectionText(codes: ScrapReasonCode[], selection: ScrapReasonSelection): string | undefined {
  if (codes.length > 0) {
    const detail = selection.detail.trim();
    if (detail) return detail;
    const chosen = codes.find((c) => c.id === selection.codeId);
    return chosen ? scrapCodeLabel(chosen) : undefined;
  }
  return selection.legacyReason || undefined;
}

const LEGACY_OPTIONS = SCRAP_REASONS.map((r) => ({ value: r.value, label: r.label }));

interface ScrapReasonFieldsProps {
  /** Active codes ([] = fallback mode). */
  codes: ScrapReasonCode[];
  value: ScrapReasonSelection;
  onChange: (next: ScrapReasonSelection) => void;
  disabled?: boolean;
}

export function ScrapReasonFields({ codes, value, onChange, disabled = false }: ScrapReasonFieldsProps) {
  const missingRequired = !isScrapSelectionComplete(codes, value);
  const requiredError = missingRequired ? 'Required when scrap is greater than zero.' : null;

  if (codes.length === 0) {
    // FALLBACK: the legacy hardcoded picker, unchanged semantics.
    return (
      <FormField label="Scrap reason" required error={requiredError}>
        <SelectField
          value={value.legacyReason}
          onChange={(next) => onChange({ ...value, legacyReason: String(next) })}
          options={LEGACY_OPTIONS}
          placeholder="Select a scrap reason"
          disabled={disabled}
          ariaLabel="Scrap reason"
        />
      </FormField>
    );
  }

  const codeOptions = codes.map((code) => ({ value: code.id, label: scrapCodeLabel(code) }));

  return (
    <>
      {/* SelectField doesn't take native id/aria-* props, so it isn't wired via
          FormField's render-prop spread; its own ariaLabel carries the
          accessible name (same pattern as CompleteWorkModal). */}
      <FormField label="Scrap reason" required error={requiredError}>
        <SelectField<number | ''>
          value={value.codeId ?? ''}
          onChange={(next) => onChange({ ...value, codeId: next === '' ? null : Number(next) })}
          options={codeOptions}
          placeholder="Select a scrap reason code"
          disabled={disabled}
          ariaLabel="Scrap reason"
        />
      </FormField>
      <FormField label="Scrap detail (optional)">
        {(field) => (
          <input
            {...field}
            type="text"
            maxLength={255}
            disabled={disabled}
            className="input"
            value={value.detail}
            onChange={(e) => onChange({ ...value, detail: e.target.value })}
            placeholder="What happened? (kept with the scrap record)"
          />
        )}
      </FormField>
    </>
  );
}

export default ScrapReasonFields;
