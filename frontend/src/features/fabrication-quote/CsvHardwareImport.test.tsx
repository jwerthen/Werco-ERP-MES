import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { CsvHardwareImport } from './CsvHardwareImport';
import { emptyPlan, newEvidence } from './types';
import type { QuoteFile, QuotePlan } from './types';

const plan = (): QuotePlan => ({ ...emptyPlan(), parts: [{ id: 'A', name: 'Assembly A', make_or_buy: 'make', purchase_unit_cost: null, costing_complete: true, evidence: newEvidence() }] });
const file: QuoteFile = { id: 7, file_name: 'hardware.csv', sha256: 'a'.repeat(64), analysis: { kind: 'csv', status: 'review_required', table: { rows: [
  { row_number: 1, cells: ['manufacturer', 'mpn', 'quantity_per_part'] },
  { row_number: 2, cells: ['Acme', '00012', '4'] },
] } } };

test('requires a visible mapped preview before appending unreviewed hardware', () => {
  const changed = jest.fn();
  render(<CsvHardwareImport file={file} plan={plan()} onChange={changed} editable />);
  expect(screen.queryByRole('button', { name: /Append/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Target made part when CSV part ID is blank'), { target: { value: 'A' } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview mapped rows' }));
  expect(screen.getByText('Acme / 00012')).toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Append 1 hardware rows' }));
  expect(changed).toHaveBeenCalledTimes(1);
  const next = changed.mock.calls[0][0] as QuotePlan;
  expect(next.hardware[0]).toMatchObject({ manufacturer: 'Acme', mpn: '00012', quantity_per_part: '4', offer: null, evidence: { reviewed: false } });
  expect(screen.getByRole('status')).toHaveTextContent('unreviewed hardware rows');
});

test('mapping changes discard preview, and externally changed quote disables stale application', () => {
  const p = plan(); const changed = jest.fn();
  const { rerender } = render(<CsvHardwareImport file={file} plan={p} onChange={changed} editable />);
  fireEvent.change(screen.getByLabelText('Target made part when CSV part ID is blank'), { target: { value: 'A' } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview mapped rows' }));
  const newer = { ...p, currency: 'CAD' };
  rerender(<CsvHardwareImport file={file} plan={newer} onChange={changed} editable />);
  expect(screen.getByRole('button', { name: 'Append 1 hardware rows' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('quote changed');
  fireEvent.change(screen.getByLabelText('Manufacturer column'), { target: { value: '' } });
  expect(screen.queryByRole('button', { name: 'Append 1 hardware rows' })).not.toBeInTheDocument();
});

test('read-only view disables import and changing files resets mappings and previews', () => {
  const changed = jest.fn(); const p = plan();
  const { rerender } = render(<CsvHardwareImport file={file} plan={p} onChange={changed} editable={false} />);
  expect(screen.getByRole('button', { name: 'Preview mapped rows' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Preview mapped rows' }));
  expect(changed).not.toHaveBeenCalled();
  rerender(<CsvHardwareImport file={file} plan={p} onChange={changed} editable />);
  fireEvent.change(screen.getByLabelText('Target made part when CSV part ID is blank'), { target: { value: 'A' } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview mapped rows' }));
  rerender(<CsvHardwareImport file={{ ...file, id: 8, sha256: 'b'.repeat(64) }} plan={p} onChange={changed} editable />);
  expect(screen.queryByRole('button', { name: 'Append 1 hardware rows' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Target made part when CSV part ID is blank')).toHaveValue('');
});
