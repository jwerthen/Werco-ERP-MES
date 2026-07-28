/**
 * BOM Unit Mismatches — the pre-arming remediation worklist for automatic
 * backflush (`Part.backflush_components`).
 *
 * `unit_of_measure_mismatch` is a BLOCKING readiness diagnostic: nothing in the
 * platform converts units, so a BOM line stating `each` against a part stocked
 * in `sheets` would issue the wrong quantity of the right material. Until every
 * disagreeing line is corrected by hand, the assembly cannot be armed. This
 * screen is how a human finds them.
 *
 * Three deliberate properties, none of them incidental:
 *
 * 1. **It is READ-ONLY and it hands off.** There is no inline BOM-line editor
 *    here on purpose: BOM-line create/update/delete currently write NO audit
 *    rows at all (a known, filed gap). Making this screen the primary
 *    remediation flow would route every correction through an un-audited
 *    endpoint and make that gap load-bearing on a compliance-critical path.
 *    Each row therefore deep-links to the existing BOM surface (`/bom?id=…`)
 *    and to the assembly part; the correction happens there, where it always
 *    has. If BOM-line writes ever gain audit coverage, that is the decision to
 *    revisit — not this file.
 *
 * 2. **`truncated` is surfaced, loudly.** The server scan has a candidate
 *    ceiling; when it is hit, `total` is a FLOOR, not a count. Rendering it as
 *    a plain total would lie about how much work is left, so the count is
 *    prefixed `≥` and a banner says why and what to do instead — and, because a
 *    truncated scan can also return an EMPTY page, the empty state under it says
 *    the scan was incomplete rather than that the shop is clean. A warning
 *    printed directly above an all-clear is not a warning.
 *
 * 3. **`blocks_backflush` answers the LINE, not the tree.** A line inside a
 *    `make` sub-assembly reports true here and still refuses nothing when the
 *    parent assembly is armed. The column is labelled "Line effect" and the
 *    caveat is on screen, because the authoritative per-part answer is the
 *    readiness check on the part itself (`GET /parts/{id}/backflush-readiness`,
 *    surfaced by the backflush card on the part page) — linked from every row.
 *
 * 4. **"No rows" is not one story.** The unfiltered empty copy is a CONCLUSION
 *    about the shop, so it is earned only on page 1 of a complete, unfiltered
 *    scan. A filtered page, a truncated scan and a page past the end of the
 *    worklist each get their own copy (see `emptyState`) — the last one because
 *    `page` is durable URL state that outlives the rows it was written against,
 *    and `DataTable` drops the pager along with the rows, so that state needs
 *    its own way out.
 *
 * Paging is SERVER-side (the endpoint is offset/limit with a `total`), so the
 * table takes `serverPagination` and client sort is deliberately unavailable —
 * sorting one page of a server-paged set reorders a window, not the worklist.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowPathRoundedSquareIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  MagnifyingGlassIcon,
  ScaleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import {
  Breadcrumbs,
  Button,
  DataTable,
  DataTableColumn,
  DataTableEmpty,
  MobileDataCard,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { getBreadcrumbParent } from '../utils/routeMeta';
import type { BOMLineUomMismatch, BOMUomMismatchReport, Part } from '../types';

/** Server default is 100 (max 500); 50 keeps the page scannable. */
const PAGE_SIZE = 50;

/**
 * Parse a positive-integer URL param, or null. Anything else — `1.1`, `-3`,
 * `abc`, an empty string — is not a value, so the caller falls back rather than
 * forwarding it. Used for the ids AND for `page`: a lax page parse sent
 * `?page=1.1` on as `skip: 5.000000000000001` (FastAPI's `skip: int` 422s it, so
 * a hand-typed URL rendered an error instead of a worklist) and `?page=2.5` as a
 * silently non-aligned `skip: 75` — a window straddling two pages.
 */
function positiveIntParam(params: URLSearchParams, key: string): number | null {
  const raw = params.get(key);
  if (!raw) return null;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : null;
}

/**
 * What the per-row "Part readiness" hand-off can and cannot show.
 *
 * The authoritative per-part verdict lives on the automatic-backflush card, and
 * `showsBackflushCard` renders that card only for a part that can carry a BOM
 * (`manufactured` / `assembly`) or one already armed. Nothing in the data model
 * forbids a BOM hanging off a part typed `purchased` — and a report of BOM data
 * defects is exactly where such a record surfaces — so that row's link would
 * open a part page with no readiness card on it. The link stays (the part is
 * still where the answer would be), but it says what to expect rather than
 * promising a verdict that may not be rendered.
 */
const PART_READINESS_HINT =
  'Opens the assembly part. The automatic-backflush readiness card appears there for a part typed ' +
  'manufactured or assembly, or one already armed — an assembly typed purchased opens without one, ' +
  'and its part type is what to correct first.';

/* -------------------------------------------------------------------------- */
/* Part filter                                                                 */
/* -------------------------------------------------------------------------- */

interface PartFilterProps {
  /** DOM id prefix — the label's `htmlFor` target. */
  id: string;
  label: string;
  hint: string;
  placeholder: string;
  selectedId: number | null;
  onSelect: (partId: number | null) => void;
}

/**
 * Debounced part search feeding one of the report's id filters.
 *
 * The endpoint takes `part_id` / `component_part_id` as raw integers, which is
 * not something a human carries around, so this resolves a part number to an id
 * and shows the resolved chip. `active_only: false` on the search: a mismatch
 * can name an inactive or soft-deleted component, and a picker that could not
 * select one would be unable to filter to the very rows this report exists to
 * disclose.
 */
function PartFilter({ id, label, hint, placeholder, selectedId, onSelect }: PartFilterProps) {
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebouncedValue(query, 250);
  const [results, setResults] = useState<Part[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchFailed, setSearchFailed] = useState(false);
  const [selectedLabel, setSelectedLabel] = useState<string | null>(null);

  // Resolve the chip label for an id that arrived from the URL (a deep link or
  // a reload), so the filter never reads as an opaque number. A failed lookup
  // degrades to "Part #id" rather than clearing a filter the user did set.
  useEffect(() => {
    if (selectedId == null) {
      setSelectedLabel(null);
      return;
    }
    let cancelled = false;
    api
      .getPart(selectedId)
      .then((part) => {
        if (!cancelled) setSelectedLabel(part.part_number || `Part #${selectedId}`);
      })
      .catch(() => {
        if (!cancelled) setSelectedLabel(`Part #${selectedId}`);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  useEffect(() => {
    const term = debouncedQuery.trim();
    if (selectedId != null || term.length < 2) {
      setResults([]);
      setSearching(false);
      setSearchFailed(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    setSearchFailed(false);
    api
      .getParts({ search: term, active_only: false, limit: 8 })
      .then((parts) => {
        if (!cancelled) setResults(parts);
      })
      .catch(() => {
        if (!cancelled) {
          setResults([]);
          setSearchFailed(true);
        }
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedQuery, selectedId]);

  if (selectedId != null) {
    return (
      <div className="min-w-[15rem]">
        <span className="label block">{label}</span>
        <div className="flex items-center gap-2 rounded-sm border border-fd-line bg-fd-sunken px-2.5 py-2">
          <span className="font-mono text-sm text-fd-ink truncate">
            {selectedLabel ?? `Part #${selectedId}`}
          </span>
          <button
            type="button"
            onClick={() => {
              setQuery('');
              onSelect(null);
            }}
            className="ml-auto text-fd-mute hover:text-fd-ink transition-colors"
            aria-label={`Clear ${label} filter`}
          >
            <XMarkIcon className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <p className="mt-1 text-[11px] text-fd-mute">{hint}</p>
      </div>
    );
  }

  return (
    <div className="relative min-w-[15rem]">
      <label htmlFor={id} className="label">
        {label}
      </label>
      <div className="relative">
        <MagnifyingGlassIcon
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fd-mute"
          aria-hidden="true"
        />
        <input
          id={id}
          type="text"
          className="input pl-9"
          value={query}
          placeholder={placeholder}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              setQuery('');
              setResults([]);
            }
          }}
        />
      </div>
      <p className="mt-1 text-[11px] text-fd-mute">{hint}</p>

      <div aria-live="polite" className="sr-only">
        {searching ? 'Searching parts' : results.length > 0 ? `${results.length} parts found` : ''}
      </div>

      {searchFailed && (
        <p className="mt-1 text-[11px] text-fd-red">Couldn&apos;t search parts. Try again.</p>
      )}

      {!searching && !searchFailed && debouncedQuery.trim().length >= 2 && results.length === 0 && (
        <p className="mt-1 text-[11px] text-fd-mute">
          No parts match &ldquo;{debouncedQuery.trim()}&rdquo;.
        </p>
      )}

      {results.length > 0 && (
        <ul className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-sm border border-fd-line bg-fd-panel shadow-lg">
          {results.map((part) => (
            <li key={part.id}>
              <button
                type="button"
                className="flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left hover:bg-white/[0.04] transition-colors"
                onClick={() => {
                  setQuery('');
                  setResults([]);
                  onSelect(part.id);
                }}
              >
                <span className="font-mono text-xs text-fd-ink">{part.part_number}</span>
                {part.name && <span className="text-[11px] text-fd-mute truncate">{part.name}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                        */
/* -------------------------------------------------------------------------- */

export default function BOMUomMismatches() {
  const [searchParams, setSearchParams] = useSearchParams();

  // Filter + page state lives in the URL so a reload, a bookmark, or a link
  // handed to whoever owns the assembly reproduces the same worklist.
  const partId = positiveIntParam(searchParams, 'part_id');
  const bomIdFilter = positiveIntParam(searchParams, 'bom_id');
  const componentPartId = positiveIntParam(searchParams, 'component_part_id');
  const activeOnly = searchParams.get('active_only') !== '0';
  // Same strict guard as the ids: a page is a positive integer or it is page 1.
  const page = positiveIntParam(searchParams, 'page') ?? 1;

  const [report, setReport] = useState<BOMUomMismatchReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  // The BOM-id box is a raw integer (there is no natural picker for a BOM), so
  // it is debounced like any free-text filter rather than refetching per keypress.
  const [bomIdDraft, setBomIdDraft] = useState(bomIdFilter == null ? '' : String(bomIdFilter));
  const debouncedBomIdDraft = useDebouncedValue(bomIdDraft, 350);

  const hasFilters = partId != null || bomIdFilter != null || componentPartId != null || !activeOnly;

  /** Write filter/page state back to the URL. Any filter change resets to page 1. */
  const updateParams = useCallback(
    (changes: Record<string, string | null>, { resetPage = true }: { resetPage?: boolean } = {}) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          Object.entries(changes).forEach(([key, value]) => {
            if (value === null || value === '') next.delete(key);
            else next.set(key, value);
          });
          if (resetPage) next.delete('page');
          return next;
        },
        { replace: true }
      );
    },
    [setSearchParams]
  );

  // Keep the box in sync when the URL's `bom_id` changes from anywhere else
  // (a deep link, Clear filters, browser navigation). Only runs when the URL
  // value actually changes, and keeps an in-progress draft that already parses
  // to the same id — so it never clobbers what someone is typing.
  useEffect(() => {
    setBomIdDraft((draft) => {
      const trimmed = draft.trim();
      const parsed = Number(trimmed);
      const draftId = trimmed && Number.isInteger(parsed) && parsed > 0 ? parsed : null;
      if (draftId === bomIdFilter) return draft;
      return bomIdFilter == null ? '' : String(bomIdFilter);
    });
  }, [bomIdFilter]);

  // Push the debounced BOM-id box into the URL (and drop it when emptied).
  useEffect(() => {
    // Only ever push a SETTLED debounce. This effect also re-runs whenever
    // `searchParams` changes from elsewhere (Clear filters, a deep link, browser
    // Back), and at that instant `debouncedBomIdDraft` can still hold the value
    // the box had BEFORE that change — pushing it would re-assert the filter the
    // user just cleared (Clear filters then leaves an empty box querying a BOM
    // id, and the sync effect above repopulates the box from it: the filter
    // cannot be cleared at all). When the debounce has not caught up with the
    // box, do nothing; the timer fires within `delayMs` and this runs again.
    if (debouncedBomIdDraft !== bomIdDraft) return;
    const trimmed = debouncedBomIdDraft.trim();
    const parsed = Number(trimmed);
    const nextValue = trimmed && Number.isInteger(parsed) && parsed > 0 ? String(parsed) : null;
    const current = searchParams.get('bom_id');
    if ((current ?? null) === nextValue) return;
    // An unparseable draft ("12a", "-3") clears the filter rather than silently
    // querying something else.
    updateParams({ bom_id: nextValue });
  }, [debouncedBomIdDraft, bomIdDraft, searchParams, updateParams]);

  /**
   * Identity of the query a report describes.
   *
   * Load-bearing, not bookkeeping. Every honesty gate below asks a question about
   * THIS page ("are we past the end?", "is the shop clean?"), and answering it
   * against a report fetched for a DIFFERENT page reconstitutes exactly the lie
   * this screen exists to prevent — with no hand-edited URL required. Click Next
   * then Prev quickly: if the page-3 response resolves last, `report` says
   * `{total: 40, returned: 0, items: []}` while `page` is 1, and an ungated clean
   * branch prints "nothing is blocking a part" beside an amber count of 40.
   *
   * So a report is only ever read as an answer while its stamp matches the query
   * on screen; otherwise the table stays in its loading state, which is the
   * truthful description of that moment.
   */
  const queryStamp = useMemo(
    () => JSON.stringify([partId, bomIdFilter, componentPartId, activeOnly, page]),
    [partId, bomIdFilter, componentPartId, activeOnly, page]
  );
  const [reportStamp, setReportStamp] = useState<string | null>(null);
  // Monotonic request id: a superseded response must never overwrite a newer one,
  // regardless of the order the network returns them in.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    const stamp = queryStamp;
    setLoading(true);
    setLoadError(false);
    try {
      const data = await api.getBOMUomMismatches({
        ...(partId != null ? { part_id: partId } : {}),
        ...(bomIdFilter != null ? { bom_id: bomIdFilter } : {}),
        ...(componentPartId != null ? { component_part_id: componentPartId } : {}),
        active_only: activeOnly,
        skip: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setReport(data);
      setReportStamp(stamp);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      console.error('Failed to load BOM unit mismatches:', err);
      setReport(null);
      setReportStamp(stamp);
      setLoadError(true);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [partId, bomIdFilter, componentPartId, activeOnly, page, queryStamp]);

  useEffect(() => {
    load();
  }, [load]);

  const items = useMemo(() => report?.items ?? [], [report]);
  const total = report?.total ?? 0;
  const truncated = report?.truncated ?? false;
  /**
   * `busy` — not merely "a request is open", but "no report on hand answers the
   * question currently on screen". It covers the in-flight case AND the window
   * between a page/filter change and its response landing, where the previous
   * report is still in state and describes something else. Everything that draws
   * a CONCLUSION gates on this; the skeleton is the honest render for both.
   */
  const busy = loading || reportStamp !== queryStamp;
  // Held false while busy: `report` still describes the PREVIOUS
  // page then, and an enabled Next computed from it could skip a page.
  const hasNext =
    report && !busy ? (page - 1) * PAGE_SIZE + report.returned < report.total : false;
  const blockingOnPage = useMemo(() => items.filter((row) => row.blocks_backflush).length, [items]);

  /**
   * This page window is past the end of the worklist.
   *
   * `page` is durable URL state — bookmarked, shared with whoever owns the
   * assembly, retained across an active-company switch — while the row count
   * under it moves as people remediate. So an in-range page becomes out-of-range
   * without anyone touching the pager: hold `?page=2` while the list is worked
   * down to 40 rows and the server answers, correctly, `{total: 40, returned: 0,
   * items: []}`. Zero rows is the same SHAPE as clean, and the clean copy is the
   * one sentence this screen exists not to say when the data does not support
   * it. It is a dead end as well as a lie: `DataTable` replaces the whole
   * container — pager included — with the empty state, and every `updateParams`
   * is `replace: true`, so Back leaves the screen. Hence a distinct state
   * carrying its own way out, rather than a silent redirect that would hide that
   * the link was stale.
   */
  const pastEnd = !busy && !loadError && report !== null && items.length === 0 && page > 1;

  const clearFilters = useCallback(() => {
    setBomIdDraft('');
    setSearchParams(new URLSearchParams(), { replace: true });
  }, [setSearchParams]);

  const goToFirstPage = useCallback(() => {
    updateParams({ page: null }, { resetPage: false });
  }, [updateParams]);

  /**
   * Which "no rows" story is true — most-qualified first.
   *
   * The unfiltered copy is the only one shaped like a CONCLUSION about the shop
   * ("nothing is blocking a part from being armed"), and it is only earned when
   * this page is the whole answer: page 1 of a complete scan with no filter
   * narrowing it. Each branch above it is a reason that it is not — an
   * out-of-range window, a scan that stopped at its own ceiling, a filter that
   * does not follow nested BOMs. Truncation outranks the filter copy because
   * "nothing here disagrees" is a conclusion too, and an incomplete scan cannot
   * support it either.
   */
  const emptyState = useMemo<DataTableEmpty>(() => {
    if (pastEnd) {
      let description: string;
      if (truncated) {
        description =
          `Page ${page} is past the last row this scan returned — and the scan stopped at its own ` +
          'candidate ceiling, so it never saw the whole list. Nothing here says a part is clean. Go ' +
          'back to page 1 and narrow the filters.';
      } else if (total === 0) {
        description =
          `Page ${page} is past the end — under the current filters this worklist has no rows at ` +
          'all. Page 1 carries the real answer.';
      } else {
        description =
          `Page ${page} is past the last row of ${total.toLocaleString()} mismatched ` +
          `${total === 1 ? 'line' : 'lines'}. Rows may have been corrected, or the filters changed, ` +
          'since this link was made. This is an out-of-range page, not an empty worklist.';
      }
      return {
        icon: ExclamationTriangleIcon,
        title: 'Past the end of this worklist',
        description,
        action: { label: 'Back to page 1', onClick: goToFirstPage },
      };
    }
    if (truncated) {
      return {
        icon: ExclamationTriangleIcon,
        title: 'Scan incomplete — this page is not an all-clear',
        description:
          'The scan stopped at its candidate ceiling, so it never saw the whole list. An empty page ' +
          'here means this window held nothing — not that every BOM line agrees, and not that any ' +
          'part is safe to arm. Narrow the filters (one assembly or one component at a time) and run ' +
          'it again.',
        ...(hasFilters ? { action: { label: 'Clear filters', onClick: clearFilters } } : {}),
      };
    }
    if (hasFilters) {
      return {
        icon: CheckCircleIcon,
        title: 'No mismatches match these filters',
        description:
          'Nothing here disagrees. Clear the filters to see the unfiltered worklist — that is the authoritative one, because an assembly filter does not follow nested sub-assembly BOMs.',
        action: { label: 'Clear filters', onClick: clearFilters },
      };
    }
    return {
      icon: CheckCircleIcon,
      title: 'No unit-of-measure mismatches',
      description:
        'Every BOM line states the unit its component part is stocked in. Nothing here is blocking a part from being armed for automatic backflush.',
    };
  }, [pastEnd, truncated, hasFilters, page, total, goToFirstPage, clearFilters]);

  const columns = useMemo<Array<DataTableColumn<BOMLineUomMismatch>>>(
    () => [
      {
        key: 'assembly',
        header: 'Assembly / BOM',
        accessor: (row) => row.part_number,
        csv: (row) => `${row.part_number} (part ${row.part_id}, bom ${row.bom_id})`,
        render: (row) => (
          <div className="min-w-0">
            <Link
              to={`/bom?id=${row.bom_id}`}
              className="font-mono text-sm text-fd-blue hover:underline"
            >
              {row.part_number}
            </Link>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-fd-mute">
              {row.bom_revision && <span>Rev {row.bom_revision}</span>}
              {row.bom_status && <span className="uppercase tracking-wide">{row.bom_status}</span>}
              {!row.bom_is_active && (
                <span className="rounded-sm border border-fd-line px-1 py-px font-mono uppercase tracking-wide text-fd-mute">
                  Inactive BOM
                </span>
              )}
            </div>
          </div>
        ),
      },
      {
        key: 'item_number',
        header: 'Line',
        align: 'right',
        className: 'tabular-nums whitespace-nowrap',
        accessor: (row) => row.item_number,
        render: (row) => (row.item_number == null ? '—' : row.item_number),
      },
      {
        key: 'component',
        header: 'Component',
        accessor: (row) => row.component_part_number,
        csv: (row) =>
          `${row.component_part_number}${row.component_is_deleted ? ' (DELETED)' : ''} (part ${row.component_part_id})`,
        render: (row) => (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-sm text-fd-ink">{row.component_part_number}</span>
              {row.component_is_deleted && (
                <span
                  className="rounded-sm border border-fd-red/40 bg-fd-red/10 px-1.5 py-px font-mono text-[10px] uppercase tracking-wide text-fd-red"
                  title="This component part is soft-deleted. It is listed on purpose — the readiness explosion still resolves it, so the line still blocks."
                >
                  Deleted part
                </span>
              )}
            </div>
            {row.component_part_name && (
              <div className="mt-0.5 truncate text-[11px] text-fd-mute">{row.component_part_name}</div>
            )}
          </div>
        ),
      },
      {
        key: 'line_unit_of_measure',
        header: 'BOM line says',
        className: 'whitespace-nowrap',
        accessor: (row) => row.line_unit_of_measure,
        render: (row) => (
          <span className="rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-1.5 py-0.5 font-mono text-xs text-fd-amber">
            {row.line_unit_of_measure}
          </span>
        ),
      },
      {
        key: 'component_unit_of_measure',
        header: 'Part stocked in',
        className: 'whitespace-nowrap',
        accessor: (row) => row.component_unit_of_measure,
        render: (row) => (
          <span className="rounded-sm border border-fd-line bg-fd-sunken px-1.5 py-0.5 font-mono text-xs text-fd-ink">
            {row.component_unit_of_measure}
          </span>
        ),
      },
      {
        key: 'blocks_backflush',
        header: 'Line effect',
        className: 'whitespace-nowrap',
        accessor: (row) => (row.blocks_backflush ? 'Would be issued' : 'Never issued'),
        render: (row) =>
          row.blocks_backflush ? (
            <span
              className="rounded-sm border border-fd-red/40 bg-fd-red/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-fd-red"
              title="The backflush would issue this line. Whether it blocks a given part is answered by that part's readiness check, not by this row."
            >
              Would be issued
            </span>
          ) : (
            <span
              className="rounded-sm border border-fd-line px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-fd-mute"
              title="Alternate / optional / reference line — the backflush never issues it, so it refuses nothing. Cosmetic."
            >
              Never issued
            </span>
          ),
      },
      {
        key: 'actions',
        header: 'Fix it in',
        className: 'whitespace-nowrap',
        render: (row) => (
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <Link to={`/bom?id=${row.bom_id}`} className="text-fd-blue hover:underline">
              BOM line
            </Link>
            <Link
              to={`/parts/${row.part_id}`}
              className="text-fd-blue hover:underline"
              title={PART_READINESS_HINT}
            >
              Part readiness
            </Link>
          </div>
        ),
      },
    ],
    []
  );

  const renderMobileCard = (row: BOMLineUomMismatch) => (
    <MobileDataCard
      key={row.bom_item_id}
      title={row.part_number}
      subtitle={`Line ${row.item_number ?? '—'} · ${row.component_part_number}`}
      badge={
        row.component_is_deleted ? (
          <span className="rounded-sm border border-fd-red/40 bg-fd-red/10 px-1.5 py-px font-mono text-[10px] uppercase tracking-wide text-fd-red">
            Deleted part
          </span>
        ) : undefined
      }
      fields={[
        { label: 'BOM line says', value: row.line_unit_of_measure },
        { label: 'Part stocked in', value: row.component_unit_of_measure },
        {
          label: 'Line effect',
          value: row.blocks_backflush ? 'Would be issued' : 'Never issued',
        },
        {
          label: 'BOM',
          value: `Rev ${row.bom_revision ?? '—'}${row.bom_is_active ? '' : ' (inactive)'}`,
        },
        {
          label: 'Fix it in',
          fullWidth: true,
          value: (
            <span className="flex items-center gap-3">
              <Link to={`/bom?id=${row.bom_id}`} className="text-fd-blue hover:underline">
                BOM line
              </Link>
              <Link
                to={`/parts/${row.part_id}`}
                className="text-fd-blue hover:underline"
                title={PART_READINESS_HINT}
              >
                Part readiness
              </Link>
            </span>
          ),
        },
      ]}
    />
  );

  // Parent crumb resolved from the shared route source (`routeMeta`), like the
  // other detail routes — one place owns the label/href, so the trail cannot
  // drift from the sidebar entry and the top-bar title.
  const bomParent = getBreadcrumbParent('/bom/uom-mismatches') ?? {
    label: 'Bill of Materials',
    href: '/bom',
  };

  return (
    <div className="space-y-4">
      <Breadcrumbs crumbs={[bomParent, { label: 'Unit Mismatches' }]} />

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <ScaleIcon className="h-7 w-7 flex-shrink-0 text-fd-blue" aria-hidden="true" />
          <div>
            <h1 className="text-2xl font-bold text-fd-ink">BOM Unit Mismatches</h1>
            <p className="max-w-3xl text-sm text-fd-mute">
              BOM lines whose stated unit of measure contradicts the component part&apos;s stocking
              unit. Nothing converts units, so these must be corrected by hand before a part can be
              armed for automatic backflush. This page reports; the correction happens on the BOM.
            </p>
          </div>
        </div>
        <Link to="/bom" className="btn-secondary whitespace-nowrap">
          Open Bill of Materials
        </Link>
      </div>

      {/* Truncation — `total` is a FLOOR, not a count. Say so before anything else. */}
      {truncated && (
        <div
          className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3"
          data-testid="uom-mismatch-truncated"
        >
          <div className="flex items-start gap-2.5">
            <ExclamationTriangleIcon
              className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-amber"
              aria-hidden="true"
            />
            <div className="text-sm text-fd-body">
              <p className="font-semibold text-fd-amber">
                Scan ceiling reached — this count is a floor, not a total.
              </p>
              <p className="mt-1">
                The scan stopped after its candidate limit, so there are <strong>at least</strong>{' '}
                {total.toLocaleString()} mismatched lines — possibly many more that this page has
                not seen. Do not read the number as how much work is left, and do not conclude from
                this page that a part is clean. Narrow the filters (one assembly or one component at
                a time) and run it again.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* What `Line effect` does and does not mean. */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex items-start gap-2.5">
          <InformationCircleIcon
            className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-blue"
            aria-hidden="true"
          />
          <div className="text-sm text-fd-body">
            <p className="font-semibold text-fd-ink">
              &ldquo;Line effect&rdquo; answers the line, not the whole tree.
            </p>
            <p className="mt-1">
              <em>Would be issued</em> means the backflush would issue <em>this line</em> — it does
              not mean this row is what is blocking any particular part. A line inside a{' '}
              <span className="font-mono text-xs">make</span> sub-assembly shows{' '}
              <em>Would be issued</em> here and still refuses nothing when the parent assembly is
              armed. The authoritative per-part answer is that part&apos;s own readiness check
              (blockers on the automatic-backflush card on the part page) — the{' '}
              <em>Part readiness</em> link on every row goes straight there.
            </p>
            <p className="mt-1 text-fd-mute">
              That card is only rendered for a part typed{' '}
              <span className="font-mono text-xs">manufactured</span> or{' '}
              <span className="font-mono text-xs">assembly</span> (or one already armed). If a BOM
              listed here hangs off a part typed{' '}
              <span className="font-mono text-xs">purchased</span>, its page opens with no readiness
              card at all — there is no per-part verdict to read until the part type is corrected.
            </p>
            <p className="mt-1 text-fd-mute">
              Filtering by assembly narrows to that assembly&apos;s <strong>own</strong> BOM and does
              not follow nested sub-assembly BOMs, which a readiness check does reach — so the
              unfiltered list is the authoritative worklist.
            </p>
          </div>
        </div>
      </div>

      {/* Counts */}
      <MiniStatStrip className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <MiniStat
          icon={ScaleIcon}
          iconBg={total > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
          iconColor={total > 0 ? 'text-fd-amber' : 'text-fd-green'}
          label="Mismatched lines"
          value={busy && !report ? '—' : `${truncated ? '≥ ' : ''}${total.toLocaleString()}`}
          valueColor={total > 0 ? 'text-fd-amber' : 'text-fd-green'}
          subtitle={truncated ? 'Floor — scan ceiling hit' : 'Matching the current filters'}
        />
        <MiniStat
          icon={ArrowPathRoundedSquareIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Would be issued (this page)"
          value={busy && !report ? '—' : blockingOnPage}
          subtitle="Alternate / optional / reference lines excluded"
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Rows on this page"
          value={busy && !report ? '—' : items.length}
          subtitle={`Page ${page} · ${PAGE_SIZE} per page`}
        />
      </MiniStatStrip>

      {/* Filters */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-wrap items-start gap-3">
          <PartFilter
            id="uom-mismatch-part"
            label="Assembly part"
            hint="That assembly's own BOM only — not nested sub-assemblies."
            placeholder="Search part number…"
            selectedId={partId}
            onSelect={(id) => updateParams({ part_id: id == null ? null : String(id) })}
          />
          <PartFilter
            id="uom-mismatch-component"
            label="Component part"
            hint="Every line naming this component, on any BOM."
            placeholder="Search part number…"
            selectedId={componentPartId}
            onSelect={(id) => updateParams({ component_part_id: id == null ? null : String(id) })}
          />
          <div className="min-w-[9rem]">
            <label htmlFor="uom-mismatch-bom-id" className="label">
              BOM ID
            </label>
            <input
              id="uom-mismatch-bom-id"
              type="text"
              inputMode="numeric"
              className="input"
              value={bomIdDraft}
              placeholder="e.g. 412"
              onChange={(e) => setBomIdDraft(e.target.value)}
            />
            <p className="mt-1 text-[11px] text-fd-mute">One specific BOM.</p>
          </div>
          <div className="min-w-[12rem] pt-6">
            <div className="flex items-center gap-2">
              <input
                id="uom-mismatch-active-only"
                type="checkbox"
                className="checkbox"
                checked={activeOnly}
                onChange={(e) => updateParams({ active_only: e.target.checked ? null : '0' })}
              />
              <label htmlFor="uom-mismatch-active-only" className="text-sm text-fd-body">
                Active BOMs only
              </label>
            </div>
            <p className="mt-1 text-[11px] text-fd-mute">
              The BOMs a backflush actually reads. Uncheck to audit history too.
            </p>
          </div>
          {hasFilters && (
            <div className="pt-6">
              <Button variant="secondary" onClick={clearFilters}>
                Clear filters
              </Button>
            </div>
          )}
        </div>
      </div>

      {/* Worklist — server-paged (skip/limit + total). Client sort is off by
          design: sorting one page of a server-paged set reorders a window. */}
      <DataTable<BOMLineUomMismatch>
        columns={columns}
        data={items}
        rowKey={(row) => row.bom_item_id}
        loading={busy}
        error={loadError ? "Couldn't load the unit-mismatch worklist." : false}
        onRetry={load}
        rowClassName={(row) => (row.component_is_deleted ? 'bg-fd-red/5' : '')}
        serverPagination={{
          page,
          pageSize: PAGE_SIZE,
          hasNext,
          onPageChange: (next) => updateParams({ page: String(next) }, { resetPage: false }),
        }}
        csvExport={{ filename: 'bom-uom-mismatches' }}
        mobileCards={renderMobileCard}
        empty={emptyState}
      />
    </div>
  );
}
