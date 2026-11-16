/**
 * OperationHoursSource — the quiet "where do these actual hours come from" line on
 * the work-order detail operations table (P1.4). It renders the SERVER-derived
 * `hours_source` only; nothing is inferred client-side, and an absent field (older
 * API, other routes) or `none` renders nothing.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { OperationHoursSource } from './OperationHoursSource';

const base = {
  actual_setup_hours: 0.5,
  actual_run_hours: 2,
  recorded_setup_hours: 0.5,
  recorded_run_hours: 2,
  hours_include_backfill: false,
};

test('time entries: shows the stored actual and its source, with the measured split', () => {
  render(<OperationHoursSource operation={{ ...base, hours_source: 'time_entries' }} />);
  const line = screen.getByTestId('op-hours-source');
  expect(line).toHaveTextContent('Act 2.50 · time entries');
  expect(line.getAttribute('title')).toContain('Closed entries: setup 0.50 h, run 2.00 h.');
  expect(line.getAttribute('title')).not.toContain('without crediting');
});

test('recorded hours above the stored actual are explained, not hidden', () => {
  render(
    <OperationHoursSource
      operation={{ ...base, hours_source: 'time_entries', recorded_run_hours: 3, actual_run_hours: 2 }}
    />
  );
  expect(screen.getByTestId('op-hours-source').getAttribute('title')).toContain(
    'closed without crediting this operation'
  );
});

test('manual hours are labelled as such', () => {
  render(
    <OperationHoursSource
      operation={{ ...base, hours_source: 'manual', recorded_setup_hours: 0, recorded_run_hours: 0 }}
    />
  );
  expect(screen.getByTestId('op-hours-source')).toHaveTextContent('Act 2.50 · manual');
});

test('back-entries are flagged', () => {
  render(<OperationHoursSource operation={{ ...base, hours_source: 'time_entries', hours_include_backfill: true }} />);
  expect(screen.getByTestId('op-hours-source')).toHaveTextContent('incl. back-entry');
});

test.each([['none' as const], [undefined], [null]])('renders nothing for hours_source %p', source => {
  const { container } = render(<OperationHoursSource operation={{ ...base, hours_source: source }} />);
  expect(container).toBeEmptyDOMElement();
});
