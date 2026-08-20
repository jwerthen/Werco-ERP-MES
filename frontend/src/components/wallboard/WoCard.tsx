/**
 * One work-order card on the Foundry TV board's 4×3 grid (design handoff
 * 2026-07-22, zone 2).
 *
 * Five fixed rows — header (WO + status chip) / unit-or-part + qty / customer OR op +
 * time / machine + stop reason / progress — all keyed off classifyJob's strict
 * HELD > DOWN > BLOCKED > LATE > RUNNING > WAITING precedence. Stoppage detail is
 * JOINED client-side by the caller (downtime from work_centers by
 * work-center code, blocked age from blocked_wos by WO number) and degrades
 * to blank cells when a join misses — the design itself has blank cells.
 * Only DOWN chip dots pulse; WAITING and HELD cards carry no glow anywhere.
 *
 * HELD (WorkOrderStatus.ON_HOLD; ON_HOLD joined the wall population 2026-08-19)
 * renders grey and de-emphasized exactly like WAITING, and leads the precedence
 * so a held WO that also happens to be blocked, down or late still reads HELD —
 * it is deliberately stopped and somebody already knows, so it must not spend
 * the alarm channel. Its stop-reason cell shows the bare words ON HOLD and
 * NOTHING ELSE: no hold reason, no NCR title, no free text. The Z3 ON HOLD
 * panel is counts-and-ages only precisely because that text can name customers
 * and suppliers, and putting a held WO on the grid is a POPULATION change, not
 * a disclosure-category change.
 *
 * WIDTH IS A CONTRACT (2026-08-20). Every row is two cells competing for ONE
 * content box — 314px at 1080p: the 347px grid cell, less the 0.25rem status
 * edge, the 0.0625rem right hairline, and 2 × 0.875rem of padding. The rule for
 * which cell yields is NOT "whichever sits on the right":
 *
 *   A cell may take its full max-content width (`shrink-0`) only when
 *   truncating it would make it LIE.
 *
 * The status chip (`LATE 12D`), the qty (`40/120`) and the time value (`2H14M`)
 * are exactly that class — a clipped number reads as a different, WRONG number,
 * so `100/250` must never render as `100/25`. All three are also small and
 * bounded (≤128px), so they keep `shrink-0` and their rows are paid for out of
 * tracking instead.
 *
 * The stop reason was the one rigid cell that was neither bounded nor
 * lie-prone, and it is the widest cell on the card: `ENGINEERING QUESTION` is
 * 211px, 69% of the box. Being `shrink-0` it took all of that, and the
 * work-center name lived on the leftovers — 87px, EIGHT characters, which is
 * how `DEBURR BENCH 1` reached the TV as `DEBURR B…`. Severity was INVERTED:
 * the states where knowing which machine matters most (DOWN, BLOCKED) were the
 * only two that hid it, while a calm LATE card — whose right cell is empty —
 * handed the same name 298px. Row 4 now inverts the priority instead: the work
 * center takes what it needs up to a 12.5rem cap and the reason absorbs the
 * deficit, because the reason is a CLOSED VOCABULARY read from the FRONT (each
 * member of DowntimeCategory, and each member of WorkOrderBlockerCategory, is
 * unique within 8 characters OF ITS OWN VOCABULARY, so `ENGINEERING…` still
 * names the blocker) while a work-center name is free text whose disambiguator
 * is at the END (`DEBURR BENCH 1` vs `… 2`, `AMADA HG 1003`) and truncates to
 * nothing at all. Per-vocabulary is the right frame and not a hedge: a card
 * draws this cell from exactly ONE of the two enums, and which one is settled
 * by the status chip and the status edge before the text is read. Across the
 * union the claim would be false — `OTHER` is in both enums and `MATERIAL`
 * (downtime) shares eight characters with `MATERIAL MISSING` (blocker).
 *
 * The 12.5rem cap is what stops the inversion from merely flipping the unfairness:
 * it guarantees the reason 308 − 200 = 108px ≈ 10 characters, and it is applied
 * ONLY when a reason exists, so a LATE/RUNNING card still gives the machine name
 * the whole row. Both cells carry `min-w-0`, which is load-bearing rather than
 * tidy: a flex item's automatic minimum size is its longest WORD, so without it
 * the reason refuses to shrink past `ENGINEERING` (111px) and the ROW OVERFLOWS
 * THE CARD — a geometry break, not a truncation, and this board's central rule
 * is that every panel keeps its slot at all data values.
 *
 * Tracking is now a LABEL affordance only. The status chip and the `UNIT `
 * prefix keep theirs (they are chrome that reads as a label); the stop reason
 * keeps a reduced 0.03em; DATA strings — WO number, part number, unit number,
 * op line, customer, work center — run at 0. On this monospace face
 * (0.6em advance) tracking is the cheapest width on the card: 0.8px per
 * character at 1rem versus 0.6px for a whole 0.0625rem font step, and it costs
 * no glyph height, which is what actually carries at 3–6m.
 *
 * Row 4 could NOT have been fixed by typography at any size — the realistic
 * worst pair (a 19-char work-center name beside `ENGINEERING QUESTION`) overruns
 * by 110.8px while every non-font lever combined yields 46.4px, and even
 * 0.875rem type with zero tracking on both sides is still 17.6px short. That
 * arithmetic is why this row needed the negotiation change and not a smaller
 * font. What it does NOT justify is swapping the name for `work_center_code`:
 * see "Machine identity" in docs/WALLBOARD.md for why that is an open owner
 * question rather than a layout decision.
 */

import React from 'react';
import type { WallboardBlockedWorkOrder, WallboardDowntime, WallboardJob } from '../../types/wallboard';
import {
  blockerLabel,
  classifyJob,
  formatAgeHours,
  formatDownDuration,
  JobStateClass,
} from '../../utils/wallboardLayout';
import { FD } from './wallboardTokens';

interface StateSpec {
  edge: string;
  chipColor: string;
  chipBg: string;
  chipEdge: string;
  dotGlow: string | null;
  barFill: string;
  barGlow: string | null;
  pulse: boolean;
}

/** Exact per-state tints from the handoff (chip bg ~12% / edge ~40%). */
const STATE_SPECS: Record<JobStateClass, StateSpec> = {
  // Identical to `waiting` on purpose: a known, deliberate stop is not an alarm.
  // No dot glow, no bar glow, and pulse:false — a held card must NEVER take the
  // DOWN red wash or the 1.6s fdPulse.
  held: {
    edge: FD.faint,
    chipColor: FD.waiting,
    chipBg: 'rgba(139,152,165,0.08)',
    chipEdge: 'rgba(139,152,165,0.28)',
    dotGlow: null,
    barFill: FD.waiting,
    barGlow: null,
    pulse: false,
  },
  down: {
    edge: FD.red,
    chipColor: FD.red,
    chipBg: 'rgba(240,68,56,0.14)',
    chipEdge: 'rgba(240,68,56,0.45)',
    dotGlow: '0 0 0.5rem rgba(240,68,56,0.9)',
    barFill: FD.red,
    barGlow: '0 0 0.5rem rgba(240,68,56,0.5)',
    pulse: true,
  },
  blocked: {
    edge: FD.blockedOrange,
    chipColor: FD.blockedOrange,
    chipBg: 'rgba(234,125,44,0.12)',
    chipEdge: 'rgba(234,125,44,0.4)',
    dotGlow: '0 0 0.4375rem rgba(234,125,44,0.8)',
    barFill: FD.blockedOrange,
    barGlow: '0 0 0.5rem rgba(234,125,44,0.4)',
    pulse: false,
  },
  late: {
    edge: FD.amber,
    chipColor: FD.amber,
    chipBg: 'rgba(210,153,34,0.12)',
    chipEdge: 'rgba(210,153,34,0.4)',
    dotGlow: '0 0 0.4375rem rgba(210,153,34,0.8)',
    barFill: FD.amber,
    barGlow: '0 0 0.5rem rgba(210,153,34,0.4)',
    pulse: false,
  },
  running: {
    edge: FD.green,
    chipColor: FD.green,
    chipBg: 'rgba(63,185,80,0.10)',
    chipEdge: 'rgba(63,185,80,0.38)',
    dotGlow: '0 0 0.4375rem rgba(63,185,80,0.8)',
    barFill: FD.green,
    barGlow: '0 0 0.5rem rgba(63,185,80,0.5)',
    pulse: false,
  },
  waiting: {
    edge: FD.faint,
    chipColor: FD.waiting,
    chipBg: 'rgba(139,152,165,0.08)',
    chipEdge: 'rgba(139,152,165,0.28)',
    dotGlow: null,
    barFill: FD.waiting,
    barGlow: null,
    pulse: false,
  },
};

function chipWord(state: JobStateClass, job: WallboardJob): string {
  switch (state) {
    case 'held':
      return 'HELD';
    case 'down':
      return 'DOWN';
    case 'blocked':
      return 'BLOCKED';
    case 'late':
      return `LATE ${job.days_late ?? 0}D`;
    case 'running':
      return 'RUNNING';
    default:
      return 'WAITING';
  }
}

export default function WoCard({
  job,
  downtime,
  blockedInfo,
  extraMinutes,
}: {
  job: WallboardJob;
  /** Open downtime on the current op's work center (join by wc code), if any. */
  downtime: WallboardDowntime | null;
  /** The WO's row in blocked_wos (join by wo_number), if any. */
  blockedInfo: WallboardBlockedWorkOrder | null;
  /** Whole minutes since the last good poll — counters tick between polls. */
  extraMinutes: number;
}) {
  const state = classifyJob(job);
  const spec = STATE_SPECS[state];
  // HELD is rendered de-emphasized "like WAITING" — same grey text ramp, so the
  // two known-quiet states read identically at 5m and neither competes with an
  // actionable alarm for attention.
  const deemphasized = state === 'waiting' || state === 'held';

  // Gated customer name (executive displays only). Blank/absent → fall back to
  // the op line, which is what every public shop-floor board shows.
  const customer = job.customer_name?.trim() || null;

  // Build identity. When a job tracks a unit, THAT is the number somebody reads off
  // the wall — the part number on these jobs is a 28-character string that truncates
  // and is unreadable at distance anyway. So the unit takes row 2's large slot and the
  // part number steps down beneath it; with no unit, row 2 is byte-identical to before.
  const unit = job.unit_number?.trim() || null;

  const qtyOrdered = job.qty_ordered ?? 0;
  const qtyComplete = job.qty_complete ?? 0;
  const pct = qtyOrdered > 0 ? Math.min(100, Math.max(0, Math.round((100 * qtyComplete) / qtyOrdered))) : 0;

  const elapsed = job.current_op
    ? formatDownDuration((job.current_op.elapsed_minutes ?? 0) + extraMinutes).toUpperCase()
    : null;

  // Op-row right: the state's time value (blank when the state has none or a
  // join missed — a blank cell is part of the design).
  let timeValue: { text: string; color: string; bold: boolean } | null = null;
  if (state === 'down' && downtime) {
    timeValue = { text: formatDownDuration(downtime.minutes + extraMinutes).toUpperCase(), color: FD.red, bold: true };
  } else if (state === 'blocked' && blockedInfo) {
    timeValue = { text: formatAgeHours(blockedInfo.age_hours).toUpperCase(), color: FD.blockedOrange, bold: true };
  } else if (state === 'running' && elapsed) {
    timeValue = { text: elapsed, color: FD.green, bold: true };
  } else if (state === 'late' && job.running && elapsed) {
    timeValue = { text: elapsed, color: FD.body, bold: false };
  }

  // Machine-row right: the stop reason.
  let reason: { text: string; color: string; bold: boolean } | null = null;
  if (state === 'down' && downtime) {
    reason = { text: blockerLabel(downtime.category).toUpperCase(), color: FD.red, bold: true };
  } else if (state === 'blocked' && blockedInfo) {
    reason = { text: blockerLabel(blockedInfo.category).toUpperCase(), color: FD.blockedOrange, bold: true };
  } else if (state === 'held') {
    // Bare words only. NO hold reason / NCR title / free text — see the header.
    reason = { text: 'ON HOLD', color: FD.mute, bold: false };
  } else if (state === 'waiting') {
    reason = { text: 'IN QUEUE', color: FD.mute, bold: false };
  }

  // px 1.125rem → 0.875rem buys 8px of content box on EVERY row at once — the
  // cheapest width on the card, and the only lever that helps all five rows.
  // `py` is deliberately UNTOUCHED: the five row heights and the card's slot in
  // the fixed 4×3 grid must not move.
  return (
    <div
      data-testid={`wo-card-${job.wo_number}`}
      className="flex min-w-0 flex-col justify-between rounded-[0.25rem] px-[0.875rem] py-[1rem]"
      style={{
        background:
          state === 'down'
            ? `linear-gradient(165deg, rgba(240,68,56,0.10), rgba(240,68,56,0.02) 60%), ${FD.panel}`
            : FD.panel,
        border: `0.0625rem solid ${state === 'down' ? 'rgba(240,68,56,0.45)' : FD.line}`,
        borderLeft: `0.25rem solid ${spec.edge}`,
      }}
    >
      {/* Row 1 — WO number + status chip.

          This row clips in PRODUCTION and was never reported, because the old
          mock fixture used an 8-char `WO-26001` while `_generate_work_order_number`
          mints the 15-char `WO-YYYYMMDD-NNN`. Against a `LATE 12D` chip the WO
          number — the card's primary identity — was 18.45px short. It is fixed
          here with no font-size change at all: the number's own tracking (a DATA
          string) goes to 0, and the chip pays the rest out of its label chrome.
          The chip keeps `shrink-0`: `LATE 12D` truncated to `LATE 1` would name
          a different, wrong age. */}
      <div className="flex items-center justify-between gap-[0.375rem]">
        <span className="min-w-0 truncate text-[1.3125rem] font-semibold tracking-normal" style={{ color: FD.body }}>
          {job.wo_number}
        </span>
        <span
          className="flex shrink-0 items-center gap-[0.375rem] rounded-[0.1875rem] px-[0.5rem] py-[0.3125rem] text-[0.875rem] font-bold tracking-[0.08em]"
          style={{ color: spec.chipColor, background: spec.chipBg, border: `0.0625rem solid ${spec.chipEdge}` }}
        >
          <span
            className="h-[0.5rem] w-[0.5rem] rounded-full"
            style={{
              background: spec.chipColor,
              boxShadow: spec.dotGlow ?? 'none',
              animation: spec.pulse ? 'fdPulse 1.6s ease-in-out infinite' : 'none',
            }}
          />
          {chipWord(state, job)}
        </span>
      </div>

      {/* Row 2 — unit # (when tracked) or part number, + qty done/total.

          The identity slot keeps its 1.9375rem size — it is the card's hero and
          the one thing a 6m glance must land on. Its deficit was never really
          about the part number anyway: the SAME 14-char `COVER-PNT-1120` is
          5.7px short beside qty `0/24` and 85.5px short beside `12500/25000`, so
          the aggressor is the QTY DIGIT COUNT. The qty may not truncate (a
          clipped count is a wrong count), so it is the one cell on the card that
          pays with font size — 1.1875rem → 1.0625rem. It still reads larger than
          the op/machine rows, and the same progress is restated as a percent on
          row 5 directly beneath it. */}
      <div className="flex items-baseline justify-between gap-[0.375rem]">
        {unit ? (
          <span className="flex min-w-0 flex-col">
            <span
              data-testid="wo-card-unit"
              className="min-w-0 truncate text-[1.9375rem] font-extrabold tracking-[-0.04em]"
              style={{ color: FD.cyan }}
            >
              <span className="text-[1.0625rem] font-bold tracking-[0.12em] opacity-75">UNIT </span>
              {unit}
            </span>
            {job.part_number ? (
              <span
                className="min-w-0 truncate text-[1.0625rem] font-semibold"
                style={{ color: deemphasized ? FD.mute : FD.body }}
              >
                {job.part_number}
              </span>
            ) : null}
          </span>
        ) : (
          <span
            className="min-w-0 truncate text-[1.9375rem] font-extrabold tracking-[-0.04em]"
            style={{ color: deemphasized ? FD.body : FD.ink }}
          >
            {job.part_number ?? ''}
          </span>
        )}
        <span className="shrink-0 text-[1.0625rem] font-medium" style={{ color: FD.mute }}>
          <span className="font-bold" style={{ color: deemphasized ? FD.body : FD.ink }}>
            {qtyComplete}
          </span>
          /{qtyOrdered}
        </span>
      </div>

      {/* Row 3 — customer (executive boards) or op position + the state's time
          value. customer_name is gated server-side: present only on an
          authorized display, so a public board falls back to the op line.

          The time value KEEPS `shrink-0` and is not the aggressor here:
          formatDownDuration / formatAgeHours cap it at 5 characters (`2H14M`,
          `38H`, `6D`) = 48px, 16% of the row, and a half-read duration is a
          wrong duration. This row still clips long operation names — see "What
          still truncates" in docs/WALLBOARD.md — because closing it would mean
          shortening the `OP n/total · ` prefix, which changes what the row SAYS
          rather than how it is laid out. */}
      <div className="flex items-center justify-between gap-[0.375rem] text-[1rem]">
        {customer ? (
          <span
            className="min-w-0 truncate font-semibold tracking-normal"
            data-testid="wo-card-customer"
            style={{ color: FD.body }}
          >
            {customer.toUpperCase()}
          </span>
        ) : (
          <span
            className="min-w-0 truncate tracking-normal"
            style={{ color: job.current_op ? (deemphasized ? FD.mute : FD.body) : FD.mute }}
          >
            {job.current_op
              ? `OP ${(job.ops_completed ?? 0) + 1}/${job.ops_total ?? 0} · ${(job.current_op.name ?? '').toUpperCase()}`
              : 'ALL OPS COMPLETE'}
          </span>
        )}
        {timeValue ? (
          <span
            className={`shrink-0 ${timeValue.bold ? 'font-bold' : 'font-semibold'}`}
            style={{ color: timeValue.color }}
          >
            {timeValue.text}
          </span>
        ) : (
          <span />
        )}
      </div>

      {/* Row 4 — machine + stop reason. THE ROW THIS CHANGE EXISTS FOR.

          Read the WIDTH IS A CONTRACT block at the top of this file first: the
          rigid cell here is the WORK CENTER, not the stop reason, and that is
          the whole fix. The machine name is free text disambiguated at its END,
          so truncating it destroys the identity; the reason is a closed enum
          whose members are unique within 8 characters of their own vocabulary,
          so truncating it does not.

          `shrink-0 max-w-[12.5rem]` is applied ONLY when a reason exists. On a
          LATE/RUNNING card the right cell is an empty <span /> and the name
          should have the entire row, so a cap there would take width away for
          nothing. 12.5rem = 200px reserves the reason 308 − 200 = 108px ≈ 10
          characters, and lets the machine cell show 20 characters whole — which
          covers every work-center name in the seed data (the longest,
          "Powder Coating Line", is 19) and every real machine in the shop bar
          one. `min-w-0` on the reason is REQUIRED, not decorative: without it
          the cell's automatic minimum size is its longest word (`ENGINEERING`,
          111px) and the ROW OVERFLOWS THE CARD instead of truncating.

          This is the ONE row whose gap GREW (0.5rem → 0.75rem) while every
          other row's shrank, and the reason is a consequence of the fix. Before,
          the machine name almost always truncated, so an ELLIPSIS sat between
          the two cells and did the separating. Now it almost always renders
          complete and ends on a real glyph, leaving two same-size monospace
          strings abutting — `DEBURR BENCH 1` and `ENGINEERING QUES…` read as one
          run-on at 5m with only 6px between them. 0.75rem is ~1.25 characters, a
          clear word break, and it still leaves the reason 314 − 12 − 200 = 102px
          ≈ 10 characters. Row 3 keeps the tighter gap precisely because its left
          cell DOES still truncate, so it supplies its own ellipsis. */}
      <div className="flex items-center justify-between gap-[0.75rem] text-[1rem]">
        <span
          className={`min-w-0 truncate tracking-normal ${reason ? 'max-w-[12.5rem] shrink-0' : ''}`}
          style={{ color: FD.mute }}
        >
          {(job.current_op?.work_center_name ?? job.current_op?.work_center_code ?? '').toUpperCase()}
        </span>
        {reason ? (
          <span
            className={`min-w-0 truncate tracking-[0.03em] ${reason.bold ? 'font-bold' : 'font-semibold'}`}
            style={{ color: reason.color }}
          >
            {reason.text}
          </span>
        ) : (
          <span />
        )}
      </div>

      {/* Row 5 — progress bar + percent */}
      <div className="flex items-center gap-[0.625rem]">
        <div
          className="h-[0.375rem] min-w-0 flex-1 overflow-hidden rounded-[0.125rem]"
          style={{ background: FD.sunken }}
        >
          <div
            className="h-full"
            style={{ width: `${pct}%`, background: spec.barFill, boxShadow: spec.barGlow ?? 'none' }}
          />
        </div>
        <span className="shrink-0 text-[0.875rem] font-semibold" style={{ color: FD.mute }}>
          {pct}%
        </span>
      </div>
    </div>
  );
}
