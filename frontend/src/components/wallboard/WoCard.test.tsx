/**
 * WoCard — the gated customer-name row (row 3) and the HELD state on the
 * Foundry TV board tile.
 *
 * The server decides whether a tile carries `customer_name` (executive
 * displays / privileged roles only). This component test pins the RENDER
 * contract that follows from that gate:
 *   - a non-blank customer_name renders in the dedicated `wo-card-customer`
 *     cell, uppercased, and takes the row over the op line;
 *   - a null / undefined / blank customer_name falls back to the op line
 *     (`OP n/total · NAME`, or `ALL OPS COMPLETE`) — the public-board default.
 *
 * HELD (2026-08-19) is the second contract pinned here. ON_HOLD work orders
 * joined the wall population, they carry the hold on the EXISTING `status`
 * field (no new wire key), and three things about the tile are decisions rather
 * than styling:
 *   - HELD LEADS the precedence — a held WO that is also down, blocked, late or
 *     running still reads HELD. It is deliberately stopped and somebody already
 *     knows, so naming any other condition on it would be misleading;
 *   - it renders GREY and de-emphasized, identically to WAITING, and must NEVER
 *     pulse or take the DOWN red wash. Leading the precedence is exactly what
 *     guarantees that, which is why the precedence test and the no-pulse test
 *     are the same claim from two directions;
 *   - its stop-reason cell shows the bare words ON HOLD and NOTHING ELSE. The Z3
 *     ON HOLD panel is counts-and-ages only precisely because hold reasons and
 *     NCR titles can name customers and suppliers, so putting held work on the
 *     grid stays a POPULATION change, not a disclosure-category change.
 *
 * WoCard is pure (no API/context), so it renders in isolation with no mocks.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import WoCard from './WoCard';
import { FD } from './wallboardTokens';
import type { WallboardJob } from '../../types/wallboard';

/** A RUNNING tile with a current op — so the op-line fallback is available and
 *  we can prove the customer cell takes precedence over it when present. */
function makeJob(overrides: Partial<WallboardJob> = {}): WallboardJob {
  return {
    wo_number: 'WO-2001',
    part_number: 'PN-88231',
    status: 'in_progress',
    qty_complete: 3,
    qty_ordered: 10,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 2,
    ops_total: 6,
    current_op: {
      sequence: 30,
      name: 'CNC Mill',
      work_center_code: 'MILL-1',
      work_center_name: 'Haas VF-4',
      status: 'in_progress',
      elapsed_minutes: 12,
      crew: [],
      crew_count: 0,
    },
    ...overrides,
  };
}

function renderCard(job: WallboardJob) {
  return render(<WoCard job={job} downtime={null} blockedInfo={null} extraMinutes={0} />);
}

describe('WoCard customer name (row 3)', () => {
  it('renders the customer name (uppercased) and hides the op line when set', () => {
    renderCard(makeJob({ customer_name: 'Globex Aerospace' }));

    const customer = screen.getByTestId('wo-card-customer');
    expect(customer).toHaveTextContent('GLOBEX AEROSPACE');
    // The op line is replaced by the customer, not shown alongside it.
    expect(screen.queryByText(/OP 3\/6 · CNC MILL/)).not.toBeInTheDocument();
  });

  it('falls back to the op line when customer_name is null (public board)', () => {
    renderCard(makeJob({ customer_name: null }));

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('falls back to the op line when customer_name is absent (undefined)', () => {
    renderCard(makeJob()); // no customer_name key at all

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('treats a blank / whitespace-only customer_name as absent (trim → op line)', () => {
    renderCard(makeJob({ customer_name: '   ' }));

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('shows the customer even when there is no current op (ALL OPS COMPLETE state)', () => {
    renderCard(makeJob({ customer_name: 'Initech', current_op: null }));

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByTestId('wo-card-customer')).toHaveTextContent('INITECH');
    // The customer takes the row, so the "ALL OPS COMPLETE" fallback is not shown.
    expect(card.queryByText('ALL OPS COMPLETE')).not.toBeInTheDocument();
  });
});

describe('WoCard HELD state (ON_HOLD on the wall, 2026-08-19)', () => {
  /** A held tile. The hold rides on `status` — there is no `held` boolean. */
  const heldJob = (overrides: Partial<WallboardJob> = {}) =>
    makeJob({ status: 'on_hold', running: false, ...overrides });

  it('reads HELD, with the bare words ON HOLD as its stop reason', () => {
    renderCard(heldJob());

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByText('HELD')).toBeInTheDocument();
    expect(card.getByText('ON HOLD')).toBeInTheDocument();
    // Not the WAITING chip/reason it would otherwise fall through to — without
    // its own chipWord case the switch default silently reads WAITING.
    expect(card.queryByText('WAITING')).not.toBeInTheDocument();
    expect(card.queryByText('IN QUEUE')).not.toBeInTheDocument();
  });

  it('wins the precedence outright — DOWN, BLOCKED, LATE and RUNNING all at once still read HELD', () => {
    renderCard(heldJob({ down: true, blocked: true, is_late: true, days_late: 9, running: true }));

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByText('HELD')).toBeInTheDocument();
    expect(card.queryByText('DOWN')).not.toBeInTheDocument();
    expect(card.queryByText('BLOCKED')).not.toBeInTheDocument();
    expect(card.queryByText(/^LATE /)).not.toBeInTheDocument();
    expect(card.queryByText('RUNNING')).not.toBeInTheDocument();
    // No time value either: a deliberate stop has no clock worth reading at 5m,
    // and the running elapsed (12m) would contradict the word HELD beside it.
    expect(card.queryByText('12M')).not.toBeInTheDocument();
  });

  it('does not pulse and never takes the DOWN red wash, even when the job IS down', () => {
    renderCard(heldJob({ down: true }));

    const card = screen.getByTestId('wo-card-WO-2001');
    // fdPulse is the board's entire motion budget and it is reserved for DOWN.
    expect(card.querySelectorAll('[style*="fdPulse"]')).toHaveLength(0);
    // The red wash (background gradient + border) is keyed on the DOWN state.
    expect(card.getAttribute('style') ?? '').not.toContain('240,68,56');
    // Grey, not an alarm colour: the left edge is WAITING's faint token.
    expect(card).toHaveStyle({ borderLeftColor: FD.faint });
    expect(screen.getByText('HELD')).toHaveStyle({ color: FD.waiting });
  });

  it('renders grey and de-emphasized EXACTLY like WAITING', () => {
    // Same job, only the status differs — so any style difference between the
    // two cards is attributable to the HELD spec and nothing else.
    const { unmount } = renderCard(makeJob({ status: 'released', running: false }));
    const waitingCardStyle = screen.getByTestId('wo-card-WO-2001').getAttribute('style');
    const waitingChipStyle = screen.getByText('WAITING').getAttribute('style');
    unmount();

    renderCard(heldJob());
    expect(screen.getByTestId('wo-card-WO-2001').getAttribute('style')).toBe(waitingCardStyle);
    expect(screen.getByText('HELD').getAttribute('style')).toBe(waitingChipStyle);
  });

  it('never borrows a stop reason from the joins — no hold reason, NCR title or free text', () => {
    // Both joins hit, and both are ignored: the card's stop-reason cell is the
    // one place a hold reason could leak onto an unattended public screen.
    render(
      <WoCard
        job={heldJob({ down: true, blocked: true })}
        downtime={{ category: 'maintenance', since: '2026-07-22T12:00:00Z', minutes: 134 }}
        blockedInfo={{ wo_number: 'WO-2001', category: 'waiting_inspect', age_hours: 22 }}
        extraMinutes={0}
      />
    );

    const card = screen.getByTestId('wo-card-WO-2001');
    expect(within(card).getByText('ON HOLD')).toBeInTheDocument();
    expect(within(card).queryByText('MAINTENANCE')).not.toBeInTheDocument();
    expect(within(card).queryByText('WAITING INSPECT')).not.toBeInTheDocument();
    expect(within(card).queryByText('2H14M')).not.toBeInTheDocument();
    expect(within(card).queryByText('22H')).not.toBeInTheDocument();
  });
});

/**
 * Row-4 width negotiation — the fix for `DEBURR BENCH 1` reaching the TV as
 * `DEBURR B…` (measured 146px of string into a 98px cell at 1920×1080).
 *
 * WHAT JSDOM CANNOT DO. Every assertion below is about DOM content and layout
 * INTENT. Real overflow is a layout property — it needs a box model, a font
 * with real metrics and a flexbox implementation, none of which jsdom has, so
 * NO test in this file proves a string actually fits on the board. That was
 * verified in a real engine instead (headless Chromium at 1920×1080, 2560×1440
 * and 3840×2160, against an adversarial 12-card fixture built from the real
 * DowntimeCategory / WorkOrderBlockerCategory vocabularies and the seed-data
 * work-center names). Those runs are the evidence; these tests are the guard
 * that stops the fix being undone, and the numbers they reference come from
 * them.
 */
describe('WoCard row 4 — machine name vs stop reason', () => {
  /** The realistic worst pair: the longest seeded work-center name (19 chars)
   *  beside the longest WorkOrderBlockerCategory member (20 chars). */
  function blockedAtPowderCoating() {
    return makeJob({
      blocked: true,
      running: false,
      current_op: {
        ...makeJob().current_op!,
        work_center_code: 'PWD-01',
        work_center_name: 'Powder Coating Line',
      },
    });
  }

  const engineeringQuestion = {
    wo_number: 'WO-2001',
    category: 'engineering_question',
    age_hours: 26,
  };

  /** Row 4 is the card's fourth child; its cells are the machine and the
   *  reason. Reached structurally ON PURPOSE — Wallboard.test.tsx matches
   *  `getAllByTestId(/^wo-card-/)` to assert card ORDER, so any `wo-card-*`
   *  testid added INSIDE a card silently joins that result and breaks it. */
  function row4(card: HTMLElement) {
    const row = card.children[3] as HTMLElement;
    return { machine: row.children[0] as HTMLElement, reason: row.children[1] as HTMLElement };
  }

  it('keeps the whole work-center name in the DOM — the row is fixed by layout, never by shortening the string', () => {
    // The rejected alternative fixed this row by rendering work_center_code
    // ("PWD-01") instead of the name. That is a CONTENT change — it decides
    // what the floor reads — and it is deliberately not what this does. If
    // someone later swaps the field, or slices the string to fit, this fails.
    render(
      <WoCard job={blockedAtPowderCoating()} downtime={null} blockedInfo={engineeringQuestion} extraMinutes={0} />
    );
    const card = screen.getByTestId('wo-card-WO-2001');
    expect(row4(card).machine).toHaveTextContent(/^POWDER COATING LINE$/);
    expect(within(card).queryByText('PWD-01')).not.toBeInTheDocument();
  });

  it('keeps the whole stop reason in the DOM as well — what it loses on screen it loses to CSS, not to JS', () => {
    // The reason is the cell that now absorbs the deficit, so it is the one
    // most likely to be "helpfully" abbreviated in code later. It must not be:
    // the ellipsis is the browser's, and a screen reader / DOM snapshot still
    // has the whole category.
    render(
      <WoCard job={blockedAtPowderCoating()} downtime={null} blockedInfo={engineeringQuestion} extraMinutes={0} />
    );
    expect(row4(screen.getByTestId('wo-card-WO-2001')).reason).toHaveTextContent(/^ENGINEERING QUESTION$/);
  });

  it('makes the STOP REASON the cell that yields, and the machine name the cell that does not', () => {
    // This is a class-shape assertion and it proves only intent, not fit —
    // see the block comment above. It exists because re-adding `shrink-0` to
    // the reason silently restores the original bug (the reason takes
    // max-content, 211px of a 306px box, and the machine name lives on the
    // 87px leftovers) and NOTHING else in this suite would fail.
    render(
      <WoCard job={blockedAtPowderCoating()} downtime={null} blockedInfo={engineeringQuestion} extraMinutes={0} />
    );
    const { machine, reason } = row4(screen.getByTestId('wo-card-WO-2001'));

    // The reason shrinks. `min-w-0` is load-bearing and not cosmetic: without
    // it a flex item's automatic minimum size is its longest WORD
    // ("ENGINEERING", 111px), so the reason refuses to shrink far enough and
    // the ROW OVERFLOWS THE CARD — a geometry break, not a truncation, and
    // this board's rule is that every panel keeps its slot at all data values.
    expect(reason.className).toContain('min-w-0');
    expect(reason.className).toContain('truncate');
    expect(reason.className).not.toContain('shrink-0');
    expect(reason.className).not.toContain('whitespace-nowrap');

    // The machine name is the rigid one, capped so it cannot starve the reason
    // in turn. 12.5rem reserves the reason ~10 characters, which is more than
    // the 8 every category needs to be unique.
    expect(machine.className).toContain('shrink-0');
    expect(machine.className).toContain('max-w-[12.5rem]');
  });

  it('drops the cap entirely when a card has no stop reason, so a calm card still gives the name the whole row', () => {
    // Severity was INVERTED before this change: DOWN and BLOCKED cards — the
    // ones where knowing which machine matters most — gave the name 87-98px,
    // while a LATE/RUNNING card whose right cell is empty gave it 298px. The
    // cap must apply only where there is something to negotiate with, or the
    // fix would take width away from the calm cards for nothing.
    render(
      <WoCard
        job={makeJob({
          current_op: {
            ...makeJob().current_op!,
            work_center_name: 'Ermaksan Fiber Laser 6KW Bay 2',
          },
        })}
        downtime={null}
        blockedInfo={null}
        extraMinutes={0}
      />
    );
    const { machine } = row4(screen.getByTestId('wo-card-WO-2001'));
    expect(machine.className).not.toContain('shrink-0');
    expect(machine.className).not.toContain('max-w-[12.5rem]');
    expect(machine).toHaveTextContent(/^ERMAKSAN FIBER LASER 6KW BAY 2$/);
  });

  it('leaves shrink-0 on the three cells where a truncation would state a WRONG number', () => {
    // The rule this change introduces is "a cell may be rigid only when
    // truncating it would make it LIE". These three qualify: a `LATE 12D` chip
    // cut to `LATE 1`, `40/120` cut to `40/12` and `26H` cut to `2` each name a
    // different, plausible, WRONG value, and a viewer has no way to tell. They
    // are also small and bounded (<=128px), so keeping them rigid is
    // affordable and their rows were paid for out of tracking instead. Pinned
    // so the negotiation change is not over-applied to them later.
    render(
      <WoCard job={blockedAtPowderCoating()} downtime={null} blockedInfo={engineeringQuestion} extraMinutes={0} />
    );
    const card = screen.getByTestId('wo-card-WO-2001');
    const chip = card.children[0].children[1] as HTMLElement;
    const qty = card.children[1].children[1] as HTMLElement;
    const blockedAge = card.children[2].children[1] as HTMLElement;
    expect(chip).toHaveTextContent('BLOCKED');
    expect(qty).toHaveTextContent('3/10');
    expect(blockedAge).toHaveTextContent('26H');
    expect(chip.className).toContain('shrink-0');
    expect(qty.className).toContain('shrink-0');
    expect(blockedAge.className).toContain('shrink-0');
  });

  it('still renders exactly five rows in every job state', () => {
    // The board's central rule is that every panel keeps its slot at all data
    // values. A width fix must not become a height fix: no row added, removed,
    // wrapped to two lines, or collapsed when a cell is empty.
    const cases: Array<[string, WallboardJob, typeof engineeringQuestion | null]> = [
      ['running', makeJob(), null],
      ['late', makeJob({ is_late: true, days_late: 12, running: false }), null],
      ['blocked', blockedAtPowderCoating(), engineeringQuestion],
      ['held', makeJob({ status: 'on_hold', running: false }), null],
      ['waiting', makeJob({ running: false }), null],
      ['no current op', makeJob({ current_op: undefined }), null],
    ];
    for (const [label, job, blockedInfo] of cases) {
      const { unmount } = render(<WoCard job={job} downtime={null} blockedInfo={blockedInfo} extraMinutes={0} />);
      expect(`${label}:${screen.getByTestId('wo-card-WO-2001').children.length}`).toBe(`${label}:5`);
      unmount();
    }
  });

  it('every stop-reason category is still unambiguous in the ~10 characters row 4 guarantees it', () => {
    // THE DESIGN DEPENDS ON THIS and it is the one row-4 property that can be
    // falsified without a browser. The machine cell's 12.5rem cap leaves the
    // reason 314 - 12 - 200 = 102px ≈ 10 characters at 1rem/0.03em, so the
    // trade "let the reason truncate, keep the machine name whole" is only
    // sound while every category is distinguishable from that prefix.
    //
    // Mirrors backend/app/models/downtime.py::DowntimeCategory and
    // work_order_blocker.py::WorkOrderBlockerCategory, plus the two literals
    // WoCard hard-codes. blockerLabel() is `category.replace(/_/g, ' ')` with
    // the caller uppercasing, so these ARE the rendered strings. If a new
    // category is added upstream that collides in its first 10 characters
    // (say ENGINEERING_REVIEW beside ENGINEERING_QUESTION), this fails and the
    // cap has to be re-derived rather than quietly rendering two blockers the
    // same. It cannot detect an addition that does NOT collide — the list is a
    // hand-kept mirror, which is the honest limit of testing an enum that
    // lives in another language.
    //
    // THE UNIQUENESS THAT MATTERS IS PER VOCABULARY, NOT ACROSS THE UNION, and
    // the two lists are therefore checked SEPARATELY. WoCard picks the cell's
    // text from exactly one enum per card — DowntimeCategory on a DOWN card,
    // WorkOrderBlockerCategory on a BLOCKED one — and the two states are told
    // apart by the status chip and the status edge before the reason is read
    // at all. Checking the union would assert something the card does not need
    // and cannot satisfy: `OTHER` is a member of BOTH enums (it renders the
    // same string in both), and `MATERIAL` (downtime) shares its first EIGHT
    // characters with `MATERIAL MISSING` (blocker). An earlier revision of
    // this test did check the union and passed only because
    // WorkOrderBlockerCategory.OTHER had been left out of the mirror — i.e. it
    // was green because the enum was under-copied, which is the one failure
    // mode a hand-kept mirror must not have. Both enums are now complete.
    const downtimeCategories = [
      'MECHANICAL',
      'ELECTRICAL',
      'TOOLING',
      'MATERIAL',
      'OPERATOR',
      'QUALITY',
      'CHANGEOVER',
      'PLANNED MAINTENANCE',
      'BREAK',
      'MEETING',
      'NO WORK',
      'OTHER',
    ];
    const blockerCategories = [
      'MATERIAL MISSING',
      'MACHINE DOWN',
      'TOOLING MISSING',
      'QUALITY HOLD',
      'LABOR UNAVAILABLE',
      'ENGINEERING QUESTION',
      'PREVIOUS OPERATION',
      'OTHER',
    ];
    // The literals belong to states of their own (HELD / WAITING), but they are
    // folded into both lists so a future category can't collide with them either.
    const literals = ['ON HOLD', 'IN QUEUE'];

    for (const [label, vocabulary] of [
      ['DowntimeCategory', [...downtimeCategories, ...literals]],
      ['WorkOrderBlockerCategory', [...blockerCategories, ...literals]],
    ] as Array<[string, string[]]>) {
      const prefixes = vocabulary.map(text => text.slice(0, 10));
      const collisions = prefixes.filter((p, i) => prefixes.indexOf(p) !== i);
      expect(`${label}:${collisions.join(',')}`).toBe(`${label}:`);
    }
  });
});
