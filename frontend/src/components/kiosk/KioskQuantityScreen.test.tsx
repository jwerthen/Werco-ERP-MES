/**
 * KioskQuantityScreen — scrap-code picker vs legacy fallback (Lean Phase 1).
 *
 * The shared REPORT PRODUCTION / COMPLETE quantity screen must:
 *  - with NO company scrap codes: keep the legacy SCRAP_REASONS tile grid and
 *    emit (reasonText, null) — the pre-codes contract the kiosk pages and
 *    their payload tests already pin;
 *  - with codes: build the grid from "CODE — Name" tiles, expose an OPTIONAL
 *    detail input, and emit (detail | null, codeId);
 *  - in both modes: block confirm until a reason is tapped when scrap > 0.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import KioskQuantityScreen from './KioskQuantityScreen';
import type { ScrapReasonCode } from '../../types/scrapReason';

const CODES: ScrapReasonCode[] = [
  { id: 7, code: 'OT', name: 'Out of tolerance', category: 'operator', description: null, is_active: true, display_order: 1 },
  { id: 9, code: 'MAT', name: 'Material defect', category: 'material', description: null, is_active: true, display_order: 2 },
];

function renderScreen(scrapCodes: ScrapReasonCode[] | null, onConfirm = jest.fn()) {
  render(
    <KioskQuantityScreen
      title="Report production"
      jobLabel="WO-1 · Op 10"
      confirmLabel="Save"
      requireTotalPositive
      scrapCodes={scrapCodes}
      busy={false}
      onConfirm={onConfirm}
      onCancel={jest.fn()}
    />
  );
  return onConfirm;
}

/** Enter 3 good then 2 scrap through the keypad. */
function enterQuantities() {
  fireEvent.click(screen.getByTestId('kiosk-key-3'));
  fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
  fireEvent.click(screen.getByTestId('kiosk-key-2'));
}

it('falls back to the legacy reason tiles and emits (text, null) with no codes', () => {
  const onConfirm = renderScreen(null);
  enterQuantities();

  // Legacy vocabulary, no detail input, confirm blocked until a tile is tapped.
  expect(screen.getByText(/scrap reason — required/i)).toBeInTheDocument();
  expect(screen.queryByTestId('kiosk-scrap-detail')).not.toBeInTheDocument();
  expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();

  fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
  fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

  expect(onConfirm).toHaveBeenCalledWith(3, 2, 'Material defect', null);
});

it('builds CODE — Name tiles from company codes and emits the code id', () => {
  const onConfirm = renderScreen(CODES);
  enterQuantities();

  expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'OT — Out of tolerance' }));
  fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

  // No detail typed -> null text, structured code id carries the reason.
  expect(onConfirm).toHaveBeenCalledWith(3, 2, null, 7);
});

it('sends the optional typed detail alongside the code id', () => {
  const onConfirm = renderScreen(CODES);
  enterQuantities();

  fireEvent.click(screen.getByRole('button', { name: 'MAT — Material defect' }));
  fireEvent.change(screen.getByTestId('kiosk-scrap-detail'), { target: { value: 'porosity on face' } });
  fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

  expect(onConfirm).toHaveBeenCalledWith(3, 2, 'porosity on face', 9);
});

it('emits (null, null) when nothing was scrapped', () => {
  const onConfirm = renderScreen(CODES);
  fireEvent.click(screen.getByTestId('kiosk-key-4'));
  fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

  expect(onConfirm).toHaveBeenCalledWith(4, 0, null, null);
});
