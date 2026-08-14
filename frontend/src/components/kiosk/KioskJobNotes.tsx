import React from 'react';
import type { KioskJobInstructions } from '../../types';

/**
 * The written guidance for one job, labeled in shop language, on every kiosk
 * surface that shows a single operation (crew-station job detail, the
 * single-operator CLOCK IN? card, and the running-job hero).
 *
 * Why one component: the note an operator needs is typed into any of five
 * different fields — two on the work order, three on the operation — and
 * before this existed NONE of them reached the floor. WO-20260807-006 carried
 * its "Unit #" in the work-order Notes field and the welder at the crew station
 * could not see it. Rendering the same block from one place is what keeps the
 * three surfaces from drifting into showing different subsets.
 *
 * Two properties are correctness, not styling:
 *
 *  1. **TEXT, never HTML.** These are operator/office-authored free text and
 *     this system deliberately has no ingest sanitizer (CLAUDE.md: "Store
 *     request bytes verbatim; escape at the sink"), so reaching for React's
 *     raw-HTML escape hatch here would turn this into a stored-XSS sink. React's
 *     own escaping is the sink. Line breaks survive via `whitespace-pre-wrap`
 *     — a CSS property, not a parse.
 *  2. **Never an empty container.** An absent field renders nothing at all — no
 *     heading, no separator, no blank row — and if all five are absent the
 *     component renders `null`. A job with no written guidance must look exactly
 *     as it did before this shipped.
 *
 * Long notes are capped and scroll INSIDE this block. The action buttons
 * (REPORT PRODUCTION / COMPLETE / JOIN-LEAVE / HOLD) sit below it on every
 * surface and an operator must always be able to reach them, so the notes
 * yield, not the verbs.
 */

/** Field order: work-order-level first, then operation-level. Labels are shop language. */
const NOTE_FIELDS: ReadonlyArray<{ key: keyof KioskJobInstructions; label: string }> = [
  { key: 'work_order_notes', label: 'Job Notes' },
  { key: 'work_order_special_instructions', label: 'Special Instructions' },
  { key: 'operation_description', label: 'Operation Detail' },
  { key: 'operation_setup_instructions', label: 'Setup' },
  { key: 'operation_run_instructions', label: 'Run' },
];

export interface KioskJobNoteEntry {
  key: keyof KioskJobInstructions;
  label: string;
  value: string;
}

/**
 * The present, non-blank fields in display order — exported so a caller can ask
 * "is there anything to show?" without rendering.
 *
 * The server normalizes whitespace-only to null, but the trim here is
 * deliberate belt-and-braces: a pre-feature backend, a cached ETag response, or
 * a hand-written fixture can still hand us `'   '`, and rendering that would
 * paint exactly the empty labeled row this component promises never to.
 */
export function kioskJobNoteEntries(job: KioskJobInstructions | null | undefined): KioskJobNoteEntry[] {
  if (!job) return [];
  const entries: KioskJobNoteEntry[] = [];
  for (const field of NOTE_FIELDS) {
    const raw = job[field.key];
    if (typeof raw !== 'string') continue;
    const value = raw.trim();
    if (!value) continue;
    entries.push({ key: field.key, label: field.label, value });
  }
  return entries;
}

interface KioskJobNotesProps {
  /** Any payload carrying the five instruction keys: a queue row or an ActiveJob. */
  job: KioskJobInstructions | null | undefined;
  /**
   * `lg` (default) — the full-screen job detail / clock-in card, read at arm's
   * length. `sm` — the running-job hero column, which shares its height with the
   * telemetry tiles and the action bar.
   */
  size?: 'lg' | 'sm';
  className?: string;
}

export default function KioskJobNotes({ job, size = 'lg', className = '' }: KioskJobNotesProps) {
  const entries = kioskJobNoteEntries(job);
  // No guidance on this job ⇒ no block. Never an empty panel.
  if (entries.length === 0) return null;

  const lg = size === 'lg';

  return (
    <section
      aria-label="Job instructions"
      data-testid="kiosk-job-notes"
      className={`rounded-[4px] border border-fd-line bg-fd-sunken ${className}`.trim()}
    >
      <p
        className={`border-b border-fd-line px-4 py-2 font-mono font-bold uppercase tracking-[0.16em] text-fd-mute ${
          lg ? 'text-[11px]' : 'text-[10px]'
        }`}
      >
        Job instructions
      </p>
      {/* Height cap + internal scroll: a long note must never push the verbs
          below the fold. `overscroll-contain` keeps a scroll gesture that runs
          out of note from dragging the whole screen underneath it. */}
      <dl
        data-testid="kiosk-job-notes-body"
        className={`overflow-y-auto overscroll-contain px-4 py-3 ${
          lg ? 'max-h-[13.5rem] space-y-3.5' : 'max-h-[8.5rem] space-y-2.5'
        }`}
      >
        {entries.map((entry) => (
          <div key={entry.key} data-testid={`kiosk-job-note-${entry.key}`}>
            <dt
              className={`font-mono font-bold uppercase tracking-[0.14em] text-fd-mute ${
                lg ? 'text-[11px]' : 'text-[10px]'
              }`}
            >
              {entry.label}
            </dt>
            {/* whitespace-pre-wrap preserves the author's line breaks; break-words
                keeps an unbroken part/serial string from widening the panel. */}
            <dd
              className={`mt-1 whitespace-pre-wrap break-words leading-snug text-fd-ink ${
                lg ? 'text-xl' : 'text-sm min-[1100px]:text-base'
              }`}
            >
              {entry.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
