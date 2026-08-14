/**
 * KioskJobNotes — the one written-guidance block shared by all three kiosk
 * surfaces that show a single operation.
 *
 * What these tests pin, and why each one is a real requirement rather than a
 * rendering detail:
 *  - every one of the five fields reaches the floor, under its SHOP label;
 *  - an absent field renders NOTHING — no heading, no separator, no blank row —
 *    and an all-absent job renders no container at all (a job with no guidance
 *    must look exactly as it did before this shipped);
 *  - the text is TEXT. This system has no ingest sanitizer by design, so markup
 *    in a note must reach the DOM as characters, never as elements;
 *  - author line breaks survive via CSS, never a parse;
 *  - a long note is capped and scrolls internally, so it can never push the
 *    action verbs off a shop tablet.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import KioskJobNotes, { kioskJobNoteEntries } from './KioskJobNotes';
import type { KioskJobInstructions } from '../../types';

const ALL_FIVE: KioskJobInstructions = {
  work_order_notes: 'Unit #4 — tag before it leaves the bay',
  work_order_special_instructions: 'Customer witness required at final',
  operation_description: 'Fit and tack the skid rails',
  operation_setup_instructions: 'Fixture B, 3/16 spacers',
  operation_run_instructions: 'Stitch weld 2 in on 6 in centers',
};

describe('KioskJobNotes', () => {
  it('renders each of the five fields under its shop-language label', () => {
    render(<KioskJobNotes job={ALL_FIVE} />);

    const cases: [keyof KioskJobInstructions, string, string][] = [
      ['work_order_notes', 'Job Notes', ALL_FIVE.work_order_notes as string],
      ['work_order_special_instructions', 'Special Instructions', ALL_FIVE.work_order_special_instructions as string],
      ['operation_description', 'Operation Detail', ALL_FIVE.operation_description as string],
      ['operation_setup_instructions', 'Setup', ALL_FIVE.operation_setup_instructions as string],
      ['operation_run_instructions', 'Run', ALL_FIVE.operation_run_instructions as string],
    ];

    cases.forEach(([key, label, value]) => {
      const block = screen.getByTestId(`kiosk-job-note-${key}`);
      // The label and the value are in the SAME block — a label rendered over
      // the wrong field would be worse than not rendering it at all.
      expect(within(block).getByText(label)).toBeInTheDocument();
      expect(within(block).getByText(value)).toBeInTheDocument();
    });
  });

  it('orders work-order-level guidance before operation-level guidance', () => {
    const { container } = render(<KioskJobNotes job={ALL_FIVE} />);
    const labels = Array.from(container.querySelectorAll('dt')).map((dt) => dt.textContent);
    expect(labels).toEqual(['Job Notes', 'Special Instructions', 'Operation Detail', 'Setup', 'Run']);
  });

  it('renders nothing at all for an absent field — no heading, no stray separator', () => {
    const { container } = render(<KioskJobNotes job={{ work_order_notes: 'Unit #4' }} />);

    expect(screen.getByText('Job Notes')).toBeInTheDocument();
    ['Special Instructions', 'Operation Detail', 'Setup', 'Run'].forEach((label) => {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    });
    // Exactly one labeled row — not four empty ones.
    expect(container.querySelectorAll('dt')).toHaveLength(1);
    expect(container.querySelectorAll('dd')).toHaveLength(1);
    expect(screen.getByTestId('kiosk-job-notes-body').children).toHaveLength(1);
  });

  it('treats explicit nulls exactly like absent keys', () => {
    render(
      <KioskJobNotes
        job={{
          work_order_notes: null,
          work_order_special_instructions: 'Customer witness required at final',
          operation_description: null,
          operation_setup_instructions: null,
          operation_run_instructions: null,
        }}
      />
    );
    expect(screen.getByText('Special Instructions')).toBeInTheDocument();
    expect(screen.queryByText('Job Notes')).not.toBeInTheDocument();
    expect(screen.getAllByRole('definition')).toHaveLength(1);
  });

  it('renders NO container when all five are absent', () => {
    const { container } = render(<KioskJobNotes job={{}} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId('kiosk-job-notes')).not.toBeInTheDocument();
  });

  it('renders no container for a null/undefined job', () => {
    const { container: nullContainer } = render(<KioskJobNotes job={null} />);
    expect(nullContainer).toBeEmptyDOMElement();
    const { container: undefContainer } = render(<KioskJobNotes job={undefined} />);
    expect(undefContainer).toBeEmptyDOMElement();
  });

  it('drops a whitespace-only value rather than painting an empty labeled row', () => {
    // The server normalizes blank to null; a stale backend or a cached response
    // can still hand us spaces, and that must not become a heading with nothing
    // under it.
    const { container } = render(
      <KioskJobNotes job={{ work_order_notes: '   \n  ', operation_run_instructions: 'Stitch weld' }} />
    );
    expect(screen.queryByText('Job Notes')).not.toBeInTheDocument();
    expect(screen.getByText('Run')).toBeInTheDocument();
    expect(container.querySelectorAll('dt')).toHaveLength(1);
  });

  it('renders markup in a note as TEXT, never as HTML', () => {
    const hostile = '<img src=x onerror="alert(1)"> <b>bold</b>';
    const { container } = render(<KioskJobNotes job={{ work_order_notes: hostile }} />);

    const value = within(screen.getByTestId('kiosk-job-note-work_order_notes')).getByRole('definition');
    expect(value.textContent).toBe(hostile);
    // No element was created from the note's contents.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  it("preserves the author's line breaks with CSS rather than by parsing", () => {
    const multiline = 'Unit #4\nUnit #5\n\nDo not stack';
    render(<KioskJobNotes job={{ work_order_notes: multiline }} />);

    const value = within(screen.getByTestId('kiosk-job-note-work_order_notes')).getByRole('definition');
    // The newlines are still characters in the DOM ...
    expect(value.textContent).toBe(multiline);
    // ... and whitespace-pre-wrap is what makes them visible. No <br> parsing.
    expect(value).toHaveClass('whitespace-pre-wrap');
    expect(value.querySelector('br')).toBeNull();
  });

  it('caps its height and scrolls internally so the action verbs stay reachable', () => {
    const wall = Array.from({ length: 60 }, (_, i) => `Line ${i + 1} of a very long shop note`).join('\n');
    const { rerender } = render(<KioskJobNotes job={{ work_order_notes: wall }} />);

    const body = () => screen.getByTestId('kiosk-job-notes-body');
    expect(body().className).toMatch(/overflow-y-auto/);
    expect(body().className).toMatch(/max-h-\[13\.5rem\]/);

    // The running-job hero shares its column with the telemetry tiles and the
    // action bar, so its cap is tighter — but it is still capped.
    rerender(<KioskJobNotes job={{ work_order_notes: wall }} size="sm" />);
    expect(body().className).toMatch(/overflow-y-auto/);
    expect(body().className).toMatch(/max-h-\[8\.5rem\]/);
  });

  it('renders the same five fields at the compact (running-job hero) size', () => {
    render(<KioskJobNotes job={ALL_FIVE} size="sm" />);
    ['Job Notes', 'Special Instructions', 'Operation Detail', 'Setup', 'Run'].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });

  it('exposes the resolved entries so a caller can ask without rendering', () => {
    expect(kioskJobNoteEntries(ALL_FIVE).map((e) => e.key)).toEqual([
      'work_order_notes',
      'work_order_special_instructions',
      'operation_description',
      'operation_setup_instructions',
      'operation_run_instructions',
    ]);
    expect(kioskJobNoteEntries({})).toEqual([]);
    expect(kioskJobNoteEntries(null)).toEqual([]);
    // The value is the STORED string, verbatim -- the trim is only the blank test.
    // The server preserves leading whitespace on purpose (indentation is layout in a
    // numbered work instruction), so trimming it back off here would undo that and
    // de-indent only the first line of a multi-line note.
    expect(kioskJobNoteEntries({ work_order_notes: '  Unit #4  ' })).toEqual([
      { key: 'work_order_notes', label: 'Job Notes', value: '  Unit #4  ' },
    ]);
    expect(kioskJobNoteEntries({ work_order_notes: '  1. Tack\n  2. Weld\n' })).toEqual([
      { key: 'work_order_notes', label: 'Job Notes', value: '  1. Tack\n  2. Weld\n' },
    ]);
  });
});
