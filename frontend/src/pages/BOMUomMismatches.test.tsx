/**
 * BOM Unit Mismatches — the pre-arming remediation worklist for automatic
 * backflush (`Part.backflush_components`).
 *
 * The assertions here are the honesty-shaped ones, not the cosmetic ones. This
 * screen's whole job is to tell a human how much work stands between them and
 * arming a part, and there are five specific ways it could lie:
 *
 *  1. **`truncated` rendered as a plain total.** The server scan has a candidate
 *     ceiling; when it is hit, `total` is a FLOOR. A number with no caveat would
 *     read as "1,200 lines to fix" when the real answer is "at least 1,200, and
 *     this page has not seen the rest" — and, worse, an empty *filtered* page
 *     would read as "this part is clean". The banner must say both.
 *  2. **`blocks_backflush` read as "this row is blocking your part".** It
 *     answers the LINE, not the tree: a line inside a `make` sub-assembly is
 *     `true` here and still refuses nothing when the parent is armed. The column
 *     is "Line effect" / *Would be issued*, the caveat is on screen, and every
 *     row links to the authoritative per-part readiness check.
 *  3. **A `false` row presented as blocking.** Alternate / optional / reference
 *     lines are cosmetic — they must read *Never issued*, never the red chip.
 *  4. **Soft-deleted components quietly listed.** They are disclosed on purpose
 *     (the readiness explosion resolves them), so they must be visibly flagged,
 *     not left looking like a part number nobody can find.
 *  5. **An empty PAGE read as an empty shop.** "No rows" has more than one
 *     cause: a filter, a scan that stopped at its own ceiling, and a `?page=`
 *     that outlived the rows it was written against (durable URL state, a
 *     remediated list, an active-company switch). Only page 1 of a complete,
 *     unfiltered scan earns the shop-wide all-clear — the other three must say
 *     what actually happened, and the out-of-range one must offer a way back,
 *     because DataTable drops the pager along with the rows. Every branch here
 *     therefore asserts the all-clear is ABSENT, not merely that a caveat is
 *     present: a warning printed above a contradicting conclusion is not a
 *     warning, and a test that only looks at the warning passes anyway.
 *
 * Plus the mechanics that keep the numbers true: server paging must REFETCH at
 * the right offset rather than slice a page client-side, a failed load must
 * render ErrorState with a Retry that actually re-runs the fetch, the empty
 * state must read as good news, and every filter must reach the request.
 *
 * Note on duplicate matches: DataTable renders BOTH the desktop table and the
 * mobile cards into jsdom (the md: breakpoint is CSS-only), so row assertions
 * are scoped to the desktop `<table data-testid="data-table">` and footer/empty
 * queries use getAll*.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import { getBreadcrumbParent } from '../utils/routeMeta';
import BOMUomMismatches from './BOMUomMismatches';
import type { BOMLineUomMismatch, BOMUomMismatchReport, Part } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getBOMUomMismatches: jest.fn(),
    // The two part-search filters resolve part number -> id, and re-resolve an
    // id that arrived from the URL back into a chip label.
    getParts: jest.fn(),
    getPart: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

/** Must match PAGE_SIZE in BOMUomMismatches.tsx. */
const PAGE_SIZE = 50;

const makeRow = (overrides: Partial<BOMLineUomMismatch> = {}): BOMLineUomMismatch => ({
  bom_id: 412,
  bom_revision: 'B',
  bom_status: 'released',
  bom_is_active: true,
  part_id: 900,
  part_number: 'ASSY-1000',
  bom_item_id: 5001,
  item_number: 10,
  component_part_id: 55,
  component_part_number: 'SHT-125-304',
  component_part_name: '.125 304 sheet',
  component_is_deleted: false,
  line_unit_of_measure: 'each',
  component_unit_of_measure: 'sheets',
  blocks_backflush: true,
  ...overrides,
});

const makeReport = (
  items: BOMLineUomMismatch[],
  overrides: Partial<BOMUomMismatchReport> = {}
): BOMUomMismatchReport => ({
  total: items.length,
  returned: items.length,
  truncated: false,
  items,
  ...overrides,
});

/** A blocking line, a cosmetic (never-issued) line, and a soft-deleted component. */
const blockingRow = makeRow();
const cosmeticRow = makeRow({
  bom_item_id: 5002,
  item_number: 20,
  component_part_id: 56,
  component_part_number: 'REF-ONLY-1',
  component_part_name: 'Reference note',
  line_unit_of_measure: 'ea',
  component_unit_of_measure: 'each',
  blocks_backflush: false,
});
const deletedComponentRow = makeRow({
  bom_item_id: 5003,
  item_number: null,
  bom_id: 413,
  bom_revision: 'A',
  bom_is_active: false,
  part_id: 901,
  part_number: 'ASSY-2000',
  component_part_id: 57,
  component_part_number: 'OBS-77',
  component_part_name: 'Obsolete stock',
  component_is_deleted: true,
  line_unit_of_measure: 'lbs',
  component_unit_of_measure: 'each',
});

const renderPage = (initialPath = '/bom/uom-mismatches') =>
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ToastProvider>
        <BOMUomMismatches />
      </ToastProvider>
    </MemoryRouter>
  );

/** The desktop table (mobile cards duplicate every value into jsdom). */
const table = () => screen.getByTestId('data-table');
const bodyRows = () => Array.from(table().querySelectorAll('tbody tr'));
/** The <tr> carrying a given cell text. */
const rowWith = (text: string) => within(table()).getByText(text).closest('tr') as HTMLElement;

const calls = () => mockedApi.getBOMUomMismatches.mock.calls;
const lastCallParams = () => calls()[calls().length - 1][0];

describe('BOMUomMismatches — rows and columns', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
  });

  it('renders the worklist columns in order, with "Line effect" as the effect column', async () => {
    renderPage();
    await screen.findByTestId('data-table');
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const headers = within(table())
      .getAllByRole('columnheader')
      .map((h) => (h.textContent || '').trim());
    expect(headers).toEqual([
      'Assembly / BOM',
      'Line',
      'Component',
      'BOM line says',
      'Part stocked in',
      'Line effect',
      'Fix it in',
    ]);
  });

  it('does not offer click-to-sort headers on a server-paged set', async () => {
    // Sorting one page of a server-paged worklist reorders a window, not the
    // list — DataTable disables client sort under `serverPagination`, so a
    // clickable header would be a control that silently does nothing.
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    within(table())
      .getAllByRole('columnheader')
      .forEach((h) => expect(h.querySelector('button')).toBeNull());
  });

  it('renders each row: assembly, line number, component, both units, and the two hand-off links', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const row = rowWith('SHT-125-304');
    // Assembly cell: part number links at the BOM, with revision + status.
    const assemblyLink = within(row).getByRole('link', { name: 'ASSY-1000' });
    expect(assemblyLink).toHaveAttribute('href', '/bom?id=412');
    expect(row).toHaveTextContent('Rev B');
    expect(row).toHaveTextContent('released');
    // Line number, component name, and the two units side by side.
    expect(row).toHaveTextContent('10');
    expect(row).toHaveTextContent('.125 304 sheet');
    expect(within(row).getByText('each')).toBeInTheDocument();
    expect(within(row).getByText('sheets')).toBeInTheDocument();
    // Hand-off: the BOM (where the correction happens) and the authoritative
    // per-part readiness check.
    expect(within(row).getByRole('link', { name: 'BOM line' })).toHaveAttribute(
      'href',
      '/bom?id=412'
    );
    expect(within(row).getByRole('link', { name: 'Part readiness' })).toHaveAttribute(
      'href',
      '/parts/900'
    );
  });

  it('renders a null item_number as an em dash and flags an inactive BOM', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const row = rowWith('OBS-77');
    expect(row).toHaveTextContent('—');
    expect(row).toHaveTextContent('Inactive BOM');
  });

  it('gives every row a Part readiness link — the authoritative per-part answer', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const links = within(table()).getAllByRole('link', { name: 'Part readiness' });
    expect(links).toHaveLength(3);
    expect(links.map((l) => l.getAttribute('href'))).toEqual([
      '/parts/900',
      '/parts/900',
      '/parts/901',
    ]);
  });

  it('labels CSV export as page-scoped, because only the current page is loaded', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));
    expect(screen.getByRole('button', { name: /export page/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /export csv/i })).not.toBeInTheDocument();
  });
});

describe('BOMUomMismatches — truncated: the count is a FLOOR, not a total', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('warns that the total is incomplete and says what to do instead', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow], { total: 5000, returned: 1, truncated: true })
    );
    renderPage();

    const banner = await screen.findByTestId('uom-mismatch-truncated');

    // 1. The number is named as a floor, not a count.
    expect(banner).toHaveTextContent(/this count is a floor, not a total/i);
    expect(banner).toHaveTextContent(/at least 5,000 mismatched lines/i);
    expect(banner).toHaveTextContent(/possibly many more that this page has not seen/i);
    // 2. The dangerous inference is refused explicitly: an incomplete scan must
    //    never be read as "this part is clean".
    expect(banner).toHaveTextContent(/do not read the number as how much work is left/i);
    expect(banner).toHaveTextContent(/do not conclude from this page that a part is clean/i);
    // 3. It says what to do about it.
    expect(banner).toHaveTextContent(/narrow the filters/i);
  });

  it('prefixes the count tile with ≥ and labels it as a floor', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow], { total: 5000, returned: 1, truncated: true })
    );
    renderPage();

    await screen.findByTestId('uom-mismatch-truncated');
    const tile = screen.getByText('Mismatched lines').closest('.card') as HTMLElement;
    expect(tile).toHaveTextContent('≥');
    expect(tile).toHaveTextContent('Floor — scan ceiling hit');
    // The bare number must never stand alone under truncation.
    expect(tile).not.toHaveTextContent('Matching the current filters');
  });

  it('shows no floor warning — and no ≥ — when the scan completed', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    expect(screen.queryByTestId('uom-mismatch-truncated')).not.toBeInTheDocument();
    const tile = screen.getByText('Mismatched lines').closest('.card') as HTMLElement;
    expect(tile).not.toHaveTextContent('≥');
    expect(tile).toHaveTextContent('Matching the current filters');
    expect(tile).toHaveTextContent('3');
  });

  it('still warns when a truncated scan returns an EMPTY page — silence is the worst lie here', async () => {
    // A truncated scan whose current page is empty is exactly the case that
    // could be misread as "nothing left to fix". The banner has to survive the
    // empty state — AND the empty state must not contradict it. A banner saying
    // "do not conclude that a part is clean" printed directly above "nothing is
    // blocking a part from being armed" is not a warning; the second sentence is
    // the one shaped like a conclusion, and it is false.
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([], { total: 5000, returned: 0, truncated: true })
    );
    renderPage();

    const banner = await screen.findByTestId('uom-mismatch-truncated');
    expect(banner).toHaveTextContent(/floor, not a total/i);
    expect(banner).toHaveTextContent(/do not conclude from this page that a part is clean/i);

    // The all-clear must be nowhere on the screen — not in the desktop empty
    // state, not in the mobile one.
    expect(
      screen.queryByText(/nothing here is blocking a part from being armed/i)
    ).not.toBeInTheDocument();
    expect(screen.queryByText('No unit-of-measure mismatches')).not.toBeInTheDocument();

    // …and the empty state says the SCAN was incomplete, not that the shop is clean.
    const empty = screen.getAllByTestId('empty-state')[0];
    expect(within(empty).getByText(/scan incomplete/i)).toBeInTheDocument();
    expect(empty).toHaveTextContent(/never saw the whole list/i);
    expect(empty).toHaveTextContent(/not that every BOM line agrees/i);
    expect(empty).toHaveTextContent(/narrow the filters/i);
  });

  it('a truncated scan with filters keeps Clear filters reachable from the empty state', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([], { total: 5000, returned: 0, truncated: true })
    );
    mockedApi.getPart.mockResolvedValue({ id: 900, part_number: 'ASSY-1000' } as Part);
    renderPage('/bom/uom-mismatches?part_id=900');

    const empty = (await screen.findAllByTestId('empty-state'))[0];
    // Truncation outranks the filtered copy — "nothing here disagrees" is a
    // conclusion too, and an incomplete scan cannot support it.
    expect(within(empty).getByText(/scan incomplete/i)).toBeInTheDocument();
    expect(empty).not.toHaveTextContent(/nothing here disagrees/i);
    // …but the way out of the filter stays on screen.
    expect(within(empty).getByRole('button', { name: 'Clear filters' })).toBeInTheDocument();
  });
});

describe('BOMUomMismatches — blocks_backflush answers the LINE, not the tree', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
  });

  it('states the caveat on screen: a sub-assembly line reads "Would be issued" and still refuses nothing', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const caveat = screen
      .getByText(/answers the line, not the whole tree/i)
      .closest('div') as HTMLElement;
    expect(caveat).toHaveTextContent(
      /does not mean this row is what is blocking any particular part/i
    );
    expect(caveat).toHaveTextContent(/still refuses nothing when the parent assembly is armed/i);
    // …and it points at the authoritative answer instead.
    expect(caveat).toHaveTextContent(/readiness check/i);
    // The filter-scope caveat: an assembly filter does not follow nested BOMs.
    expect(caveat).toHaveTextContent(/does not follow nested sub-assembly BOMs/i);
    expect(caveat).toHaveTextContent(/unfiltered list is the authoritative worklist/i);
  });

  it('renders a true row as "Would be issued" and defers the per-part verdict', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const chip = within(rowWith('SHT-125-304')).getByText('Would be issued');
    // Never phrased as "blocking your part".
    expect(chip.getAttribute('title')).toMatch(/readiness check/i);
    expect(chip.getAttribute('title')).not.toMatch(/blocking your part/i);
  });

  it('does NOT present a false row as blocking', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const row = rowWith('REF-ONLY-1');
    expect(within(row).getByText('Never issued')).toBeInTheDocument();
    expect(within(row).queryByText('Would be issued')).not.toBeInTheDocument();
    // And it says why it is cosmetic.
    expect(within(row).getByText('Never issued').getAttribute('title')).toMatch(
      /refuses nothing/i
    );
  });

  it('counts only the would-be-issued lines in the page tile, not every listed row', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const tile = screen.getByText('Would be issued (this page)').closest('.card') as HTMLElement;
    // 3 rows on the page, 2 of which the backflush would actually issue.
    expect(within(tile).getByText('2')).toBeInTheDocument();
    expect(tile).toHaveTextContent('Alternate / optional / reference lines excluded');
  });
});

describe('BOMUomMismatches — soft-deleted components are disclosed, and flagged', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
  });

  it('flags a deleted component with a chip that explains why it is listed', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const row = rowWith('OBS-77');
    const chip = within(row).getByText('Deleted part');
    expect(chip.getAttribute('title')).toMatch(/soft-deleted/i);
    expect(chip.getAttribute('title')).toMatch(/listed on purpose/i);
    expect(chip.getAttribute('title')).toMatch(/still blocks/i);
  });

  it('tints the deleted-component row, and leaves clean rows untinted', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    expect(rowWith('OBS-77').className).toContain('bg-fd-red/5');
    expect(rowWith('SHT-125-304').className).not.toContain('bg-fd-red/5');
    expect(within(rowWith('SHT-125-304')).queryByText('Deleted part')).not.toBeInTheDocument();
  });
});

describe('BOMUomMismatches — empty state reads as "nothing is blocking arming"', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(makeReport([]));
    mockedApi.getPart.mockResolvedValue({ id: 900, part_number: 'ASSY-1000' } as Part);
  });

  it('unfiltered: says every line agrees and that nothing is blocking a part from being armed', async () => {
    renderPage();

    const empty = (await screen.findAllByTestId('empty-state'))[0];
    expect(within(empty).getByText('No unit-of-measure mismatches')).toBeInTheDocument();
    expect(empty).toHaveTextContent(
      /every BOM line states the unit its component part is stocked in/i
    );
    expect(empty).toHaveTextContent(
      /nothing here is blocking a part from being armed for automatic backflush/i
    );
  });

  it('filtered: says so separately, and does NOT claim the tenant is clean', async () => {
    renderPage('/bom-uom?part_id=900');

    const empty = (await screen.findAllByTestId('empty-state'))[0];
    expect(within(empty).getByText('No mismatches match these filters')).toBeInTheDocument();
    // The dangerous inference — "so the whole shop is clean" — is refused: an
    // assembly filter does not follow nested sub-assembly BOMs.
    expect(empty).toHaveTextContent(/clear the filters to see the unfiltered worklist/i);
    expect(empty).toHaveTextContent(/does not follow nested sub-assembly BOMs/i);
    expect(empty).not.toHaveTextContent(/nothing here is blocking a part from being armed/i);
    expect(within(empty).getByRole('button', { name: 'Clear filters' })).toBeInTheDocument();
  });
});

describe('BOMUomMismatches — server pagination refetches, it does not slice', () => {
  const pageOne = Array.from({ length: PAGE_SIZE }, (_, i) =>
    makeRow({
      bom_item_id: 1000 + i,
      item_number: i + 1,
      component_part_id: 1000 + i,
      component_part_number: `P1-COMP-${i}`,
    })
  );
  const pageTwo = Array.from({ length: 20 }, (_, i) =>
    makeRow({
      bom_item_id: 2000 + i,
      item_number: i + 1,
      component_part_id: 2000 + i,
      component_part_number: `P2-COMP-${i}`,
    })
  );

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('requests skip 0 / limit PAGE_SIZE on first load', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(makeReport([blockingRow]));
    renderPage();

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
  });

  it('renders every row the server returned — the page is not re-sliced client-side', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport(pageOne, { total: 70, returned: PAGE_SIZE })
    );
    renderPage();

    await waitFor(() => expect(bodyRows()).toHaveLength(PAGE_SIZE));
    expect(within(table()).getByText('P1-COMP-0')).toBeInTheDocument();
    expect(within(table()).getByText(`P1-COMP-${PAGE_SIZE - 1}`)).toBeInTheDocument();
  });

  it('Next refetches at skip = PAGE_SIZE and swaps in the server rows (no client slice)', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageOne, { total: 70, returned: PAGE_SIZE })
    );
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(PAGE_SIZE));

    // Page 1 of 70: Prev disabled, Next enabled.
    screen
      .getAllByRole('button', { name: 'Previous page' })
      .forEach((b) => expect(b).toBeDisabled());
    screen.getAllByRole('button', { name: 'Next page' }).forEach((b) => expect(b).not.toBeDisabled());
    // Footer reflects the SERVER offset, not a client window.
    expect(screen.getAllByText(`1–${PAGE_SIZE}`).length).toBeGreaterThan(0);

    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageTwo, { total: 70, returned: 20 })
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(2));
    expect(lastCallParams()).toEqual({ active_only: true, skip: PAGE_SIZE, limit: PAGE_SIZE });

    // The rows on screen are the ones the SECOND response carried — they could
    // not have been derived from page 1's in-memory array.
    await waitFor(() => expect(bodyRows()).toHaveLength(20));
    expect(within(table()).getByText('P2-COMP-0')).toBeInTheDocument();
    expect(within(table()).queryByText('P1-COMP-0')).not.toBeInTheDocument();
    expect(screen.getAllByText(`${PAGE_SIZE + 1}–70`).length).toBeGreaterThan(0);

    // 50 + 20 === total → no further page.
    screen.getAllByRole('button', { name: 'Next page' }).forEach((b) => expect(b).toBeDisabled());
    screen
      .getAllByRole('button', { name: 'Previous page' })
      .forEach((b) => expect(b).not.toBeDisabled());
  });

  it('Prev returns to skip 0', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageOne, { total: 70, returned: PAGE_SIZE })
    );
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(PAGE_SIZE));

    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageTwo, { total: 70, returned: 20 })
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);
    await waitFor(() => expect(bodyRows()).toHaveLength(20));

    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageOne, { total: 70, returned: PAGE_SIZE })
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Previous page' })[0]);

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(3));
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
  });

  it('keeps Next disabled when the first page already carries the whole set', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow], { total: 2, returned: 2 })
    );
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(2));

    screen.getAllByRole('button', { name: 'Next page' }).forEach((b) => expect(b).toBeDisabled());
  });

  it('a page window past `total` says so — it never renders the shop-wide all-clear', async () => {
    // `page` is durable URL state, and the row count under it moves as people
    // remediate: hold `?page=2` while the list is worked down to 40 rows (or
    // switch active company with the URL retained) and the server answers,
    // correctly, with zero items against a non-zero `total`. Zero rows is the
    // same SHAPE as clean. It is not clean, and `hasFilters` — which knows
    // nothing about `page` — must not be what decides that.
    mockedApi.getBOMUomMismatches.mockResolvedValue(makeReport([], { total: 40, returned: 0 }));
    renderPage('/bom/uom-mismatches?page=2');

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
    expect(lastCallParams()).toEqual({ active_only: true, skip: PAGE_SIZE, limit: PAGE_SIZE });

    const empty = (await screen.findAllByTestId('empty-state'))[0];
    expect(within(empty).getByText('Past the end of this worklist')).toBeInTheDocument();
    expect(empty).toHaveTextContent(/page 2 is past the last row of 40 mismatched lines/i);
    expect(empty).toHaveTextContent(/not an empty worklist/i);
    // The count tile still says 40 — so the body must not say "nothing".
    expect(
      screen.queryByText(/nothing here is blocking a part from being armed/i)
    ).not.toBeInTheDocument();
    expect(screen.queryByText('No unit-of-measure mismatches')).not.toBeInTheDocument();
  });

  it('gives an out-of-range page a way back — the pager is gone along with the rows', async () => {
    const remediated = pageOne.slice(0, 40);
    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(makeReport([], { total: 40, returned: 0 }));
    renderPage('/bom/uom-mismatches?page=2');

    await screen.findAllByTestId('empty-state');
    // DataTable replaces the whole container — footer included — with the empty
    // state, so Prev/Next cannot be the escape, and every updateParams is
    // `replace: true`, so Back leaves the screen.
    expect(screen.queryAllByRole('button', { name: 'Next page' })).toHaveLength(0);
    expect(screen.queryAllByRole('button', { name: 'Previous page' })).toHaveLength(0);

    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(remediated, { total: 40, returned: 40 })
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Back to page 1' })[0]);

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(2));
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
    await waitFor(() => expect(bodyRows()).toHaveLength(40));
  });

  it('truncated + paging: a later page pages on, and an out-of-range one refuses the all-clear', async () => {
    // Page 2 of a ceiling-hit scan: rows present, banner up, Next still live
    // because the floor is far beyond this window.
    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport(pageTwo, { total: 5000, returned: 20, truncated: true })
    );
    renderPage('/bom/uom-mismatches?page=2');

    await waitFor(() => expect(bodyRows()).toHaveLength(20));
    expect(lastCallParams()).toEqual({ active_only: true, skip: PAGE_SIZE, limit: PAGE_SIZE });
    expect(screen.getByTestId('uom-mismatch-truncated')).toBeInTheDocument();
    // (2-1)*50 + 20 = 70 < 5000 → there is more to see.
    screen
      .getAllByRole('button', { name: 'Next page' })
      .forEach((b) => expect(b).not.toBeDisabled());

    // Page 3 comes back empty — past the end of what this (incomplete) scan saw.
    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport([], { total: 5000, returned: 0, truncated: true })
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);

    await waitFor(() => expect(lastCallParams()).toMatchObject({ skip: PAGE_SIZE * 2 }));
    const empty = (await screen.findAllByTestId('empty-state'))[0];
    expect(within(empty).getByText('Past the end of this worklist')).toBeInTheDocument();
    expect(empty).toHaveTextContent(/never saw the whole list/i);
    expect(empty).toHaveTextContent(/nothing here says a part is clean/i);
    // Both caveats survive together, and neither is contradicted below it.
    expect(screen.getByTestId('uom-mismatch-truncated')).toBeInTheDocument();
    expect(
      screen.queryByText(/nothing here is blocking a part from being armed/i)
    ).not.toBeInTheDocument();
  });

  it('parses ?page as strictly as the ids — a fractional page is not a page', async () => {
    // `?page=1.1` used to send `skip: 5.000000000000001`, which FastAPI's
    // `skip: int` 422s — a hand-typed URL rendering ErrorState instead of a
    // worklist — and `?page=2.5` a silently non-aligned `skip: 75`.
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow], { total: 1, returned: 1 })
    );
    renderPage('/bom/uom-mismatches?page=1.1');

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
  });

  it('falls back to page 1 for a non-aligned, zero, or unparseable ?page', async () => {
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow], { total: 1, returned: 1 })
    );

    for (const bad of ['2.5', '0', '-3', 'abc']) {
      mockedApi.getBOMUomMismatches.mockClear();
      const { unmount } = renderPage(`/bom/uom-mismatches?page=${bad}`);
      await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
      expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
      // …and the page tile agrees with what was actually requested.
      const tile = screen.getByText('Rows on this page').closest('.card') as HTMLElement;
      expect(tile).toHaveTextContent(`Page 1 · ${PAGE_SIZE} per page`);
      unmount();
    }
  });
});

describe('BOMUomMismatches — wayfinding and hand-offs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
  });

  it('resolves the parent crumb from routeMeta — one source, not a literal in the page', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    const parent = getBreadcrumbParent('/bom/uom-mismatches');
    expect(parent).not.toBeNull();
    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' });
    // Whatever routeMeta says the parent is, that is what the trail shows —
    // hardcoding it here would be a second source free to drift.
    expect(within(nav).getByRole('link', { name: parent!.label })).toHaveAttribute(
      'href',
      parent!.href
    );
    expect(within(nav).getByText('Unit Mismatches')).toBeInTheDocument();
  });

  it('says what the Part readiness hand-off can and cannot show, rather than linking into nothing', async () => {
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(3));

    // The card carrying the authoritative per-part verdict is rendered only for
    // a part typed manufactured/assembly (or one already armed). Nothing forbids
    // a BOM hanging off a `purchased` part, and a report of BOM data defects is
    // exactly where such a record shows up — so the link must not promise a
    // verdict that page may not render.
    within(table())
      .getAllByRole('link', { name: 'Part readiness' })
      .forEach((link) => {
        expect(link.getAttribute('title')).toMatch(/manufactured or assembly/i);
        expect(link.getAttribute('title')).toMatch(/purchased opens without one/i);
      });

    // …and the on-screen caveat panel — which is what sends the user there for
    // the authoritative answer — carries the same warning.
    const caveat = screen
      .getByText(/answers the line, not the whole tree/i)
      .closest('div') as HTMLElement;
    expect(caveat).toHaveTextContent(/opens with no readiness card at all/i);
    expect(caveat).toHaveTextContent(/until the part type is corrected/i);
  });
});

describe('BOMUomMismatches — failed load', () => {
  let consoleErrorSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  it('renders ErrorState (not an empty table, not a blank section)', async () => {
    mockedApi.getBOMUomMismatches.mockRejectedValue(new Error('boom'));
    renderPage();

    const errorState = await screen.findByTestId('error-state');
    expect(errorState).toHaveTextContent("Couldn't load the unit-mismatch worklist.");
    // Never a silent "no mismatches" — a failed load must not read as good news.
    expect(screen.queryByTestId('empty-state')).not.toBeInTheDocument();
    expect(screen.queryByText('No unit-of-measure mismatches')).not.toBeInTheDocument();
  });

  it('Retry actually re-runs the fetch and recovers', async () => {
    mockedApi.getBOMUomMismatches.mockRejectedValueOnce(new Error('boom'));
    renderPage();
    await screen.findByTestId('error-state');
    expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1);

    mockedApi.getBOMUomMismatches.mockResolvedValueOnce(
      makeReport([blockingRow, cosmeticRow, deletedComponentRow])
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(bodyRows()).toHaveLength(3));
    expect(screen.queryByTestId('error-state')).not.toBeInTheDocument();
  });
});

describe('BOMUomMismatches — filters drive the request params', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMUomMismatches.mockResolvedValue(makeReport([blockingRow]));
    mockedApi.getPart.mockResolvedValue({ id: 900, part_number: 'ASSY-1000' } as Part);
    mockedApi.getParts.mockResolvedValue([
      { id: 900, part_number: 'ASSY-1000', name: 'Weldment' } as Part,
    ]);
  });

  it('carries a deep-linked filter set straight into the first request', async () => {
    renderPage('/bom-uom?part_id=900&bom_id=412&component_part_id=55&active_only=0');

    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
    expect(lastCallParams()).toEqual({
      part_id: 900,
      bom_id: 412,
      component_part_id: 55,
      active_only: false,
      skip: 0,
      limit: PAGE_SIZE,
    });
  });

  it('unchecking "Active BOMs only" refetches with active_only false', async () => {
    renderPage();
    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });

    fireEvent.click(screen.getByLabelText('Active BOMs only'));

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ active_only: false, skip: 0, limit: PAGE_SIZE })
    );
  });

  it('typing a BOM ID refetches with bom_id once debounced', async () => {
    renderPage();
    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('BOM ID'), { target: { value: '412' } });

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ bom_id: 412, active_only: true, skip: 0, limit: PAGE_SIZE })
    );
  });

  it('ignores an unparseable BOM ID rather than querying something else', async () => {
    renderPage('/bom-uom?bom_id=412');
    await waitFor(() => expect(lastCallParams()).toMatchObject({ bom_id: 412 }));

    fireEvent.change(screen.getByLabelText('BOM ID'), { target: { value: '41x' } });

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE })
    );
  });

  it('the assembly picker resolves a part number to part_id and filters by it', async () => {
    renderPage();
    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('Assembly part'), { target: { value: 'ASSY' } });

    // Inactive/soft-deleted components must be selectable — those are exactly
    // the rows this report exists to disclose.
    await waitFor(() =>
      expect(mockedApi.getParts).toHaveBeenCalledWith({
        search: 'ASSY',
        active_only: false,
        limit: 8,
      })
    );

    fireEvent.click(await screen.findByRole('button', { name: /ASSY-1000/ }));

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ part_id: 900, active_only: true, skip: 0, limit: PAGE_SIZE })
    );
    // The chip shows the resolved part number, not an opaque id.
    expect(await screen.findByLabelText('Clear Assembly part filter')).toBeInTheDocument();
  });

  it('the component picker filters by component_part_id', async () => {
    mockedApi.getParts.mockResolvedValue([
      { id: 55, part_number: 'SHT-125-304', name: '.125 304 sheet' } as Part,
    ]);
    renderPage();
    await waitFor(() => expect(mockedApi.getBOMUomMismatches).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText('Component part'), { target: { value: 'SHT' } });
    await waitFor(() => expect(mockedApi.getParts).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole('button', { name: /SHT-125-304/ }));

    await waitFor(() =>
      expect(lastCallParams()).toEqual({
        component_part_id: 55,
        active_only: true,
        skip: 0,
        limit: PAGE_SIZE,
      })
    );
  });

  it('Clear filters drops every filter and refetches the authoritative unfiltered worklist', async () => {
    renderPage('/bom-uom?part_id=900&bom_id=412&component_part_id=55&active_only=0');
    await waitFor(() => expect(lastCallParams()).toMatchObject({ part_id: 900 }));

    fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]);

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE })
    );
  });

  it('Clear filters clears the BOM ID box too — the debounced filter does not resurrect itself', async () => {
    // Regression: the debounced push effect also re-runs when `searchParams`
    // changes from elsewhere. With a stale debounce still holding the old box
    // value, Clear filters re-applied bom_id immediately (and the sync effect
    // then repopulated the box from it), so the filter could not be cleared at
    // all — the screen showed an empty box while querying one BOM.
    renderPage('/bom-uom?bom_id=412');
    await waitFor(() => expect(lastCallParams()).toMatchObject({ bom_id: 412 }));
    expect(screen.getByLabelText('BOM ID')).toHaveValue('412');

    fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]);

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE })
    );
    // …and it STAYS cleared past the debounce window (350 ms), box included.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    expect(screen.getByLabelText('BOM ID')).toHaveValue('');
    expect(lastCallParams()).toEqual({ active_only: true, skip: 0, limit: PAGE_SIZE });
    expect(screen.queryAllByRole('button', { name: 'Clear filters' })).toHaveLength(0);
  });

  it('changing a filter resets to page 1 (skip 0)', async () => {
    const fullPage = Array.from({ length: PAGE_SIZE }, (_, i) =>
      makeRow({ bom_item_id: 3000 + i, component_part_id: 3000 + i, component_part_number: `C-${i}` })
    );
    mockedApi.getBOMUomMismatches.mockResolvedValue(
      makeReport(fullPage, { total: 200, returned: PAGE_SIZE })
    );
    renderPage();
    await waitFor(() => expect(bodyRows()).toHaveLength(PAGE_SIZE));

    fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);
    await waitFor(() => expect(lastCallParams()).toMatchObject({ skip: PAGE_SIZE }));

    fireEvent.click(screen.getByLabelText('Active BOMs only'));

    await waitFor(() =>
      expect(lastCallParams()).toEqual({ active_only: false, skip: 0, limit: PAGE_SIZE })
    );
  });
});

/* -------------------------------------------------------------------------- */
/* Stale responses                                                             */
/* -------------------------------------------------------------------------- */

describe('BOMUomMismatches — a superseded response is never read as an answer', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getParts.mockResolvedValue([] as unknown as Part[]);
  });

  /**
   * The out-of-order-response door onto the same lie the empty-state precedence
   * chain closes. Nothing here touches the URL by hand: Next then Prev is an
   * ordinary double-click, and if the page-2 response resolves LAST it lands a
   * report describing `{total: 40, returned: 0, items: []}` while `page` is back
   * to 1. Gated on `loading` alone that renders the shop-wide all-clear beside
   * an amber count of 40 — page 1, not out of range, so `pastEnd` is false and
   * the clean branch wins. The report is therefore only read while its stamp
   * matches the query on screen.
   */
  it('does not render the all-clear when a stale page-2 response resolves after page 1', async () => {
    const firstPage = makeReport([makeRow({ bom_item_id: 5001, component_part_number: 'RAW-1' })], {
      total: 70,
      returned: 1,
    });
    // The page-2 answer for a list that has since been remediated down to 40 rows.
    const stalePageTwo = makeReport([], { total: 40, returned: 0 });

    let releaseStale: (() => void) | undefined;
    const stalePending = new Promise<BOMUomMismatchReport>((resolve) => {
      releaseStale = () => resolve(stalePageTwo);
    });

    mockedApi.getBOMUomMismatches
      .mockResolvedValueOnce(firstPage) // page 1
      .mockReturnValueOnce(stalePending) // page 2 — held open
      .mockResolvedValueOnce(firstPage); // back to page 1, resolves first

    renderPage();
    await screen.findByText('RAW-1');

    fireEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0]);
    await waitFor(() => expect(lastCallParams()).toEqual(expect.objectContaining({ skip: PAGE_SIZE })));

    fireEvent.click(screen.getAllByRole('button', { name: 'Previous page' })[0]);
    await waitFor(() => expect(lastCallParams()).toEqual(expect.objectContaining({ skip: 0 })));
    await screen.findByText('RAW-1');

    // Now the superseded page-2 request finally answers.
    await act(async () => {
      releaseStale?.();
      await stalePending;
    });

    // It must change nothing: no all-clear, and the page-1 rows still stand.
    expect(screen.queryByText(/nothing here is blocking a part from being armed/i)).not.toBeInTheDocument();
    expect(screen.queryByText('No unit-of-measure mismatches')).not.toBeInTheDocument();
    expect(within(table()).getByText('RAW-1')).toBeInTheDocument();
  });
});
