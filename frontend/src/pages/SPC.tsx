import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import api from '../services/api';
import {
  ChartBarIcon,
  ExclamationTriangleIcon,
  BeakerIcon,
  ClipboardDocumentCheckIcon,
  PlusIcon,
  XMarkIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { Modal } from '../components/ui/Modal';
import { Button, EmptyState, ErrorState, FormField, LoadingButton, useToast } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { formatCentralDateTime } from '../utils/centralTime';
import { toDisplayString } from '../utils/apiError';
import type { Part } from '../types';
import {
  SPC_CHARACTERISTIC_TYPES,
  SPC_CHART_TYPE_LABELS,
  SPC_VIOLATION_RULE_LABELS,
  SPCCapability,
  SPCChartData,
  SPCChartPoint,
  SPCCharacteristic,
  SPCCharacteristicCreate,
  SPCControlLimit,
  SPCDashboard,
  SPCMeasurement,
  SPCMeasurementCreate,
  SPCOutOfControlAlert,
  SPCViolationsResponse,
} from '../types/spc';

/** Chart window passed explicitly rather than relying on the server default of 50. */
const CHART_WINDOW = 50;

/**
 * How many measurements the "Recent Measurements" panel shows.
 *
 * GET /spc/measurements orders `subgroup_number ASC, sample_number ASC` and THEN applies
 * `limit`, so ANY `limit` returns the OLDEST rows — and `limit` is capped at 5000, so a
 * bigger window only moves the cliff. The fetch is therefore bounded by `start_date`
 * (derived from the chart window) so the rows that come back are genuinely the recent
 * ones; see `recentWindowStart` below.
 */
const RECENT_MEASUREMENT_COUNT = 25;

/**
 * The ONLY chart type this backend can actually compute.
 *
 * `calculate_control_limits` computes X-bar/R limits (A2/D3/D4 constants keyed on
 * `subgroup_size`) for every characteristic regardless of its `chart_type`, and
 * `check_western_electric_rules` evaluates X-bar rules against them. Creating a
 * characteristic labelled "P Chart" or "Individual & MR" would therefore record a chart
 * type the system never honours, and then evaluate it under X-bar/R rules anyway — a
 * false claim on an AS9100D record. The create form offers X-bar/R only; the other six
 * values still RENDER (pre-existing rows, importers) and get an explicit advisory.
 */
const SUPPORTED_CHART_TYPE = 'xbar_r' as const;

/**
 * The server's limit calculator refuses `subgroup_size` outside 2–10 (400, "Subgroup size
 * must be between 2 and 10 for X-bar/R charts"), so a characteristic outside that range
 * can never get limits, violations, or a capability study from this backend.
 */
const MIN_SUBGROUP_SIZE = 2;
const MAX_SUBGROUP_SIZE = 10;

/** Backend column widths — over-length text is a 500 (no server-side length validation). */
const MAX_LENGTHS = { name: 255, unit_of_measure: 50, lot_number: 100, serial_number: 100 };

const CHART_COLORS = {
  grid: '#334155',
  axis: '#94a3b8',
  series: '#3b82f6',
  range: '#a78bfa',
  control: '#ef4444',
  center: '#22c55e',
  spec: '#f59e0b',
};

const fmt = (value: number | null | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '--' : String(value);

const fmtFixed = (value: number | null | undefined, digits: number): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '--' : value.toFixed(digits);

const NO_DATA_TONE = { text: 'text-slate-400', bg: 'bg-fd-raised/40 border-fd-line', label: 'No data' };

/**
 * Capability tone for a CENTRING index (Cpk / Ppk) only.
 *
 * Cp and Pp describe spread and ignore where the process sits inside the tolerance, so a
 * badly off-centre process can carry a high Cp and a failing Cpk at the same time. The
 * server's own verdict agrees: `is_capable = cpk >= 1.33` — Cpk alone. Labelling a Cp
 * tile "Capable" would therefore print a capability claim the quality system does not
 * make, right next to "Not Capable" on the Cpk tile. Cp/Pp get `SPREAD_ONLY_TONE`.
 *
 * All four indices are ALSO nullable on `CapabilityResponse`, and `null >= 1.33` is
 * false, so a missing index used to render as red "Not Capable" — a false failing claim.
 */
const capabilityTone = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_DATA_TONE;
  if (value >= 1.33) return { text: 'text-fd-green', bg: 'bg-fd-green/10 border-fd-green/30', label: 'Capable' };
  if (value >= 1.0) return { text: 'text-fd-amber', bg: 'bg-fd-amber/10 border-fd-amber/30', label: 'Marginal' };
  return { text: 'text-fd-red', bg: 'bg-fd-red/10 border-fd-red/30', label: 'Not Capable' };
};

/** Cp / Pp: a real number, but not a capability verdict. Stated, not colour-coded. */
const spreadOnlyTone = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_DATA_TONE;
  return { text: 'text-slate-200', bg: 'bg-fd-raised/40 border-fd-line', label: 'Spread only' };
};

const CAPABILITY_TILES = [
  { label: 'Cp', key: 'cp' as const, tone: spreadOnlyTone },
  { label: 'Cpk', key: 'cpk' as const, tone: capabilityTone },
  { label: 'Pp', key: 'pp' as const, tone: spreadOnlyTone },
  { label: 'Ppk', key: 'ppk' as const, tone: capabilityTone },
];

const errorDetail = (err: unknown, fallback: string): string => {
  const detail = (err as { response?: { data?: { detail?: unknown } } } | null | undefined)?.response?.data?.detail;
  const text = detail === undefined || detail === null ? '' : toDisplayString(detail);
  return text || fallback;
};

/**
 * Samples the manual-entry modal collects for a characteristic (its rational subgroup).
 *
 * Keyed on `subgroup_size` ALONE, deliberately. The A2/D3/D4 constants the server uses
 * come from `subgroup_size`, so collecting any other number of samples biases R̄ against
 * a constant chosen for a different n. An earlier version special-cased
 * `chart_type === 'individual_mr'` down to one sample, which guaranteed collapsed limits
 * (every range 0 → R̄ = 0 → UCL = CL = LCL) for exactly the characteristics whose declared
 * size the create form forced to ≥ 2 — a shape the UI offered and could not then use.
 */
const samplesForCharacteristic = (char: SPCCharacteristic | null): number =>
  char && char.subgroup_size > 1 ? char.subgroup_size : 1;

/**
 * Naive-UTC `start_date` that bounds GET /spc/measurements to roughly the newest
 * `RECENT_MEASUREMENT_COUNT` rows, or `undefined` when the whole history already fits.
 *
 * Walks the chart window newest-first, accumulating each subgroup's real `sample_count`
 * until the target is covered, and returns that subgroup's first-sample timestamp. The
 * route parses this with `datetime.fromisoformat` and compares it against a NAIVE UTC
 * column, so the trailing `Z` that `to_utc_iso` emits is stripped rather than sent.
 */
const recentWindowStart = (points: SPCChartPoint[]): string | undefined => {
  let covered = 0;
  for (let i = points.length - 1; i >= 0; i -= 1) {
    covered += Math.max(1, points[i].sample_count);
    if (covered >= RECENT_MEASUREMENT_COUNT) {
      const stamp = points[i].measured_at;
      // A null timestamp cannot bound anything; fall through to an unbounded read rather
      // than silently bounding on the wrong subgroup.
      return stamp ? stamp.replace(/Z$/, '') : undefined;
    }
  }
  return undefined;
};

interface ChartTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: SPCChartPoint }>;
  metric: 'mean' | 'range';
  unit: string | null;
}

const ChartTooltip: React.FC<ChartTooltipProps> = ({ active, payload, metric, unit }) => {
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload;
  const suffix = unit ? ` ${unit}` : '';
  return (
    <div className="rounded-sm border border-fd-line bg-fd-panel px-3 py-2 text-xs text-slate-200 shadow-lg">
      <p className="font-semibold text-slate-100">Subgroup #{point.subgroup_number}</p>
      <p className="tabular-nums">
        {metric === 'mean' ? 'Mean (X̄)' : 'Range (R)'}: {fmt(metric === 'mean' ? point.mean : point.range)}
        {suffix}
      </p>
      <p className="text-slate-400">
        {point.sample_count} sample{point.sample_count === 1 ? '' : 's'}
      </p>
      {point.measured_at && <p className="text-slate-400">{formatCentralDateTime(point.measured_at)}</p>}
      {point.violations.length > 0 && (
        <p className="mt-1 text-fd-red">Violations: {[...point.violations].sort().join(', ')}</p>
      )}
    </div>
  );
};

const SPC = () => {
  const { showToast } = useToast();
  const { user } = useAuth();
  // Recalculating control limits rewrites out-of-control flags on historical measurements,
  // so the server gates it to ADMIN/MANAGER/QUALITY (require_role also admits
  // platform_admin and superusers). The control is hidden for everyone else so the refusal
  // is not discovered through an error toast -- /spc itself is only `quality:view`, which
  // operator and viewer both hold. The server gate remains the enforcement.
  const canRecalculate =
    (!!user && ['platform_admin', 'admin', 'manager', 'quality'].includes(user.role)) || !!user?.is_superuser;

  const [stats, setStats] = useState<SPCDashboard | null>(null);
  const [characteristics, setCharacteristics] = useState<SPCCharacteristic[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [controlLimits, setControlLimits] = useState<SPCControlLimit | null>(null);
  const [capability, setCapability] = useState<SPCCapability | null>(null);
  const [measurements, setMeasurements] = useState<SPCMeasurement[]>([]);
  const [chartData, setChartData] = useState<SPCChartData | null>(null);
  const [violationsResponse, setViolationsResponse] = useState<SPCViolationsResponse | null>(null);
  const [outOfControl, setOutOfControl] = useState<SPCOutOfControlAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [dashboardError, setDashboardError] = useState(false);
  const [detailsError, setDetailsError] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  // Characteristics are listed active-only by default. Without a way back, deactivating
  // one would hide it AND its whole measurement history from the only page that reads it.
  const [showInactive, setShowInactive] = useState(false);

  const [showAddMeasurement, setShowAddMeasurement] = useState(false);
  const [showCreateChar, setShowCreateChar] = useState(false);
  const [savingMeasurement, setSavingMeasurement] = useState(false);
  const [savingChar, setSavingChar] = useState(false);

  // Parts serve two purposes: the REQUIRED `part_id` picker on the create form, and the
  // part number shown beside every characteristic name (the backend orders characteristics
  // by name only, so two "Bore Diameter" rows on different parts sort adjacent and are
  // otherwise indistinguishable). Loaded once alongside the dashboard; a failure here
  // degrades the labels and the picker, never the page.
  const [parts, setParts] = useState<Part[]>([]);
  const [partsError, setPartsError] = useState(false);

  /**
   * Guards against a slow response for characteristic A landing after the user has
   * already switched to B — which would leave B selected in the list and titled in the
   * panel while the chart, measurements and violations all showed A.
   */
  const detailsRequestRef = useRef(0);

  const [measurementForm, setMeasurementForm] = useState<{
    samples: string[];
    lot_number: string;
    serial_number: string;
    notes: string;
  }>({ samples: [''], lot_number: '', serial_number: '', notes: '' });
  const [sampleErrors, setSampleErrors] = useState<Record<number, string>>({});

  const [charForm, setCharForm] = useState<{
    name: string;
    part_id: string;
    characteristic_type: string;
    unit_of_measure: string;
    specification_nominal: string;
    specification_usl: string;
    specification_lsl: string;
    subgroup_size: string;
    is_critical: boolean;
  }>({
    name: '',
    part_id: '',
    characteristic_type: 'dimensional',
    unit_of_measure: '',
    specification_nominal: '',
    specification_usl: '',
    specification_lsl: '',
    subgroup_size: '5',
    is_critical: false,
  });
  const [charErrors, setCharErrors] = useState<Record<string, string>>({});

  const fetchDashboard = useCallback(async () => {
    setDashboardError(false);
    try {
      // Every ApiService SPC method already returns `response.data`. Use the return
      // value directly -- re-unwrapping with `.data` yields undefined, and for the
      // bare-array routes the old `?.results || .data || []` fallback collapsed to [].
      // `getSPCCharacteristics` pages to completion, so `chars.length` is a real total.
      const [dashboard, chars, ooc] = await Promise.all([
        api.getSPCDashboard(),
        api.getSPCCharacteristics(showInactive ? undefined : { is_active: true }),
        api.getSPCOutOfControl(),
      ]);
      setStats(dashboard);
      setCharacteristics(chars ?? []);
      setOutOfControl(ooc ?? []);
    } catch (err) {
      console.error('Failed to load SPC dashboard', err);
      setDashboardError(true);
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  const fetchParts = useCallback(async () => {
    setPartsError(false);
    try {
      setParts(await api.getParts({ active_only: true, item_group: 'all' }));
    } catch (err) {
      console.error('Failed to load parts', err);
      setPartsError(true);
    }
  }, []);

  useEffect(() => {
    fetchParts();
  }, [fetchParts]);

  const fetchCharacteristicDetails = useCallback(async (id: number) => {
    const requestId = detailsRequestRef.current + 1;
    detailsRequestRef.current = requestId;
    setDetailsError(false);
    try {
      // Chart data comes FIRST because the measurement read is bounded by it: without a
      // `start_date` the route returns the OLDEST rows (it orders ascending, then limits),
      // so a characteristic with more than `limit` measurements would show ancient rows
      // under a "Recent" heading, with stale out-of-control highlighting.
      const chart = await api.getSPCChartData(id, { last_n_subgroups: CHART_WINDOW });
      const startDate = recentWindowStart(chart?.chart_points ?? []);

      const [limits, cap, meas, viol] = await Promise.all([
        api.getSPCControlLimits(id),
        api.getSPCCapability(id),
        api.getSPCMeasurements(id, startDate ? { start_date: startDate } : undefined),
        api.getSPCViolations(id),
      ]);
      if (detailsRequestRef.current !== requestId) return;

      // Both control-limits and capability legitimately return `null` (200 with a JSON
      // null body) until they have been calculated -- that is data, not an error.
      setControlLimits(limits ?? null);
      setCapability(cap ?? null);
      setMeasurements(meas ?? []);
      setChartData(chart ?? null);
      setViolationsResponse(viol ?? null);
    } catch (err) {
      if (detailsRequestRef.current !== requestId) return;
      console.error('Failed to load characteristic details', err);
      setDetailsError(true);
    }
  }, []);

  useEffect(() => {
    if (selectedId) {
      fetchCharacteristicDetails(selectedId);
    }
  }, [selectedId, fetchCharacteristicDetails]);

  const selectedChar = useMemo(
    () => characteristics.find((c) => c.id === selectedId) || null,
    [characteristics, selectedId]
  );

  const partLabelById = useMemo(() => {
    const map = new Map<number, string>();
    parts.forEach((part) => map.set(part.id, part.part_number));
    return map;
  }, [parts]);

  /** Part number when it is known, the bare id when it is not — never a guess. */
  const partLabel = useCallback(
    (partId: number) => partLabelById.get(partId) ?? `Part #${partId}`,
    [partLabelById]
  );

  const chartPoints = useMemo<SPCChartPoint[]>(() => chartData?.chart_points ?? [], [chartData]);

  const chartPointBySubgroup = useMemo(() => {
    const map = new Map<number, SPCChartPoint>();
    chartPoints.forEach((point) => map.set(point.subgroup_number, point));
    return map;
  }, [chartPoints]);

  /**
   * ADVISORY next subgroup number for the modal header, derived the way the kiosk capture
   * path derives it server-side (`MAX(subgroup_number) + 1`): `chart_points` is ascending
   * and windowed to the numerically-highest N, so its LAST element carries the max.
   * Deliberately not derived from GET /measurements, which is limited from the oldest end.
   *
   * The number actually POSTED is re-derived from a fresh read at submit time — see
   * `handleAddMeasurement`. This one can be stale by however long the modal is open, and
   * the kiosk writes to the same characteristics concurrently under no lock.
   */
  const nextSubgroupNumber = useMemo(
    () => (chartPoints.length > 0 ? chartPoints[chartPoints.length - 1].subgroup_number : 0) + 1,
    [chartPoints]
  );

  const unit = selectedChar?.unit_of_measure ?? null;

  /** Newest-first slice of the fetched window (the route itself cannot order descending). */
  const recentMeasurements = useMemo(
    () => [...measurements].slice(-RECENT_MEASUREMENT_COUNT).reverse(),
    [measurements]
  );

  const violations = violationsResponse?.violations ?? [];
  const hasNoControlLimitsYet = !controlLimits;

  /** The R chart is meaningless for individuals data, where every range is 0. */
  const showRangeChart = chartPoints.some((point) => point.sample_count > 1);

  /**
   * The server can only produce X-bar/R limits, and only for 2 <= subgroup_size <= 10.
   * Outside that range POST .../calculate is a guaranteed 400, so the control is not
   * offered — same principle as not firing a capability study without spec limits.
   */
  const subgroupSizeUnsupported =
    !!selectedChar &&
    (selectedChar.subgroup_size < MIN_SUBGROUP_SIZE || selectedChar.subgroup_size > MAX_SUBGROUP_SIZE);

  /** Pre-existing / imported rows can carry a chart type this backend never honours. */
  const chartTypeUnsupported = !!selectedChar && selectedChar.chart_type !== SUPPORTED_CHART_TYPE;

  /**
   * `calculate_control_limits` takes n for the A2/D3/D4 constants from
   * `characteristic.subgroup_size` but computes R from the ACTUAL values. When every
   * subgroup holds one sample (the process-sheet capture shape) every range is 0, so
   * R̄ = 0 and UCL == LCL == CL -- and `check_western_electric_rules` early-returns on
   * UCL == LCL, CLEARING previously recorded violations. Warn before recalculating.
   */
  const collapsedLimitsRisk =
    !!selectedChar &&
    selectedChar.subgroup_size >= 2 &&
    chartPoints.length > 0 &&
    chartPoints.every((point) => point.sample_count === 1);

  const openMeasurementModal = useCallback(() => {
    setMeasurementForm({
      samples: Array.from({ length: samplesForCharacteristic(selectedChar) }, () => ''),
      lot_number: '',
      serial_number: '',
      notes: '',
    });
    setSampleErrors({});
    setShowAddMeasurement(true);
  }, [selectedChar]);

  const openCreateCharacteristic = useCallback(() => {
    setCharErrors({});
    setShowCreateChar(true);
    // Parts load with the dashboard; only retry here if that load failed.
    if (parts.length === 0 && partsError) fetchParts();
  }, [parts.length, partsError, fetchParts]);

  const handleAddMeasurement = useCallback(async () => {
    if (!selectedId) return;

    // One rational subgroup, all samples required: the A2/D3/D4 constants come from the
    // declared subgroup size, so a short subgroup biases R̄ against a constant chosen for n.
    const nextErrors: Record<number, string> = {};
    const values: number[] = [];
    measurementForm.samples.forEach((raw, idx) => {
      const trimmed = raw.trim();
      if (!trimmed) {
        nextErrors[idx] = 'Required';
        return;
      }
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed)) {
        nextErrors[idx] = 'Must be a number';
        return;
      }
      values[idx] = parsed;
    });

    if (Object.keys(nextErrors).length > 0) {
      setSampleErrors(nextErrors);
      showToast('error', 'Enter a numeric value for every sample in the subgroup.');
      return;
    }
    setSampleErrors({});

    const notes = measurementForm.notes.trim() || null;
    const lotNumber = measurementForm.lot_number.trim() || null;
    const serialNumber = measurementForm.serial_number.trim() || null;

    setSavingMeasurement(true);
    try {
      // Re-derive the subgroup number from a FRESH read, immediately before posting.
      //
      // There is no unique constraint on (characteristic_id, subgroup_number) and no 409:
      // posting a number that already exists silently merges these readings into an
      // existing rational subgroup, retroactively changing its X̄ and R and duplicating
      // `sample_number` — on a quality record. The kiosk process-sheet capture path
      // derives its own MAX+1 server-side, deliberately under no lock, and writes to the
      // same characteristics, so the value computed when this modal opened can be stale by
      // however long it stayed open. A fresh single-subgroup read narrows that to the
      // round trip. If the read fails, refuse — do not guess a subgroup number.
      const latest = await api.getSPCChartData(selectedId, { last_n_subgroups: 1 });
      const latestPoints = latest?.chart_points ?? [];
      const subgroupNumber =
        (latestPoints.length > 0 ? latestPoints[latestPoints.length - 1].subgroup_number : 0) + 1;

      // `measured_by` is NOT part of the contract -- the server stamps it from the caller's
      // token. The whole subgroup goes in ONE call: the endpoint validates every id before
      // the first insert, so a subgroup is all-or-nothing.
      const payload: SPCMeasurementCreate[] = values.map((value, idx) => ({
        characteristic_id: selectedId,
        subgroup_number: subgroupNumber,
        measurement_value: value,
        sample_number: idx + 1,
        lot_number: lotNumber,
        serial_number: serialNumber,
        notes,
      }));

      await api.addSPCMeasurements({ measurements: payload });
      setShowAddMeasurement(false);
      await Promise.all([fetchCharacteristicDetails(selectedId), fetchDashboard()]);
      showToast(
        'success',
        `Subgroup #${subgroupNumber} recorded (${payload.length} sample${payload.length === 1 ? '' : 's'}).`
      );
    } catch (err) {
      console.error('Failed to add measurement', err);
      showToast('error', errorDetail(err, 'Failed to add measurement.'));
    } finally {
      setSavingMeasurement(false);
    }
  }, [selectedId, measurementForm, fetchCharacteristicDetails, fetchDashboard, showToast]);

  const handleCreateCharacteristic = useCallback(async () => {
    const nextErrors: Record<string, string> = {};

    const name = charForm.name.trim();
    if (!name) nextErrors.name = 'Name is required.';

    // `part_id` is required server-side AND immutable afterwards (it is absent from
    // CharacteristicUpdate), so an unselected part must be refused here rather than sent
    // as parseInt('') -> NaN -> JSON null -> 422.
    const partId = Number(charForm.part_id);
    if (!charForm.part_id || !Number.isInteger(partId) || partId <= 0) {
      nextErrors.part_id = 'Select the part this characteristic belongs to.';
    }

    if (!charForm.characteristic_type) nextErrors.characteristic_type = 'Select a characteristic type.';

    const subgroupSize = Number(charForm.subgroup_size);
    if (
      !Number.isInteger(subgroupSize) ||
      subgroupSize < MIN_SUBGROUP_SIZE ||
      subgroupSize > MAX_SUBGROUP_SIZE
    ) {
      nextErrors.subgroup_size = `Must be a whole number from ${MIN_SUBGROUP_SIZE} to ${MAX_SUBGROUP_SIZE}.`;
    }

    const parseSpec = (raw: string, key: string): number | null => {
      const trimmed = raw.trim();
      if (!trimmed) return null;
      const parsed = Number(trimmed);
      if (!Number.isFinite(parsed)) {
        nextErrors[key] = 'Must be a number.';
        return null;
      }
      return parsed;
    };

    const nominal = parseSpec(charForm.specification_nominal, 'specification_nominal');
    const usl = parseSpec(charForm.specification_usl, 'specification_usl');
    const lsl = parseSpec(charForm.specification_lsl, 'specification_lsl');

    if (usl !== null && lsl !== null && usl <= lsl) {
      nextErrors.specification_usl = 'USL must be greater than LSL.';
    }

    if (Object.keys(nextErrors).length > 0) {
      setCharErrors(nextErrors);
      return;
    }
    setCharErrors({});

    const payload: SPCCharacteristicCreate = {
      name,
      part_id: partId,
      characteristic_type: charForm.characteristic_type,
      unit_of_measure: charForm.unit_of_measure.trim() || null,
      // The server names these `specification_*`; sending `nominal`/`usl`/`lsl` is not an
      // error -- pydantic `extra='ignore'` DROPS them and the characteristic is stored
      // with no spec limits, which then 400s every capability study.
      specification_nominal: nominal,
      specification_usl: usl,
      specification_lsl: lsl,
      // Fixed, not chosen: see SUPPORTED_CHART_TYPE. Sent explicitly rather than left to
      // the server default so the stored value is the one the UI showed.
      chart_type: SUPPORTED_CHART_TYPE,
      subgroup_size: subgroupSize,
      is_critical: charForm.is_critical,
    };

    setSavingChar(true);
    try {
      const created = await api.createSPCCharacteristic(payload);
      setCharForm({
        name: '',
        part_id: '',
        characteristic_type: 'dimensional',
        unit_of_measure: '',
        specification_nominal: '',
        specification_usl: '',
        specification_lsl: '',
        subgroup_size: '5',
        is_critical: false,
      });
      setShowCreateChar(false);
      await fetchDashboard();
      if (created?.id) setSelectedId(created.id);
      showToast('success', 'Characteristic created.');
    } catch (err) {
      console.error('Failed to create characteristic', err);
      showToast('error', errorDetail(err, 'Failed to create characteristic.'));
    } finally {
      setSavingChar(false);
    }
  }, [charForm, fetchDashboard, showToast]);

  const handleRecalculate = useCallback(async () => {
    if (!selectedId || !selectedChar) return;
    // A subgroup size outside 2-10 is a guaranteed 400 from the limit calculator, so it is
    // refused here with the reason rather than fired and reported as a server failure.
    if (
      selectedChar.subgroup_size < MIN_SUBGROUP_SIZE ||
      selectedChar.subgroup_size > MAX_SUBGROUP_SIZE
    ) {
      showToast(
        'error',
        `Control limits need a subgroup size of ${MIN_SUBGROUP_SIZE}-${MAX_SUBGROUP_SIZE}; this characteristic is set to ${selectedChar.subgroup_size}.`
      );
      return;
    }
    setRecalculating(true);
    try {
      await api.calculateSPCControlLimits(selectedId);
      // The capability study needs BOTH spec limits (400 otherwise), so it is attempted
      // only when they exist rather than firing a guaranteed refusal.
      const hasSpecLimits =
        selectedChar.specification_usl !== null && selectedChar.specification_lsl !== null;
      if (hasSpecLimits) {
        await api.runSPCCapabilityStudy(selectedId);
        showToast('success', 'Control limits and capability recalculated.');
      } else {
        showToast('success', 'Control limits recalculated. Set USL and LSL to run a capability study.');
      }
      await fetchCharacteristicDetails(selectedId);
    } catch (err) {
      console.error('Failed to recalculate', err);
      showToast('error', errorDetail(err, 'Failed to recalculate control limits.'));
      // Reflect only what the server actually did -- this path rewrites quality evidence,
      // so the panel is re-read rather than optimistically updated.
      await fetchCharacteristicDetails(selectedId);
    } finally {
      setRecalculating(false);
    }
  }, [selectedId, selectedChar, fetchCharacteristicDetails, showToast]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-fd-blue" />
      </div>
    );
  }

  const partOptions = [...parts].sort((a, b) => a.part_number.localeCompare(b.part_number));

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Statistical Process Control</h1>
        <Button size="sm" onClick={openCreateCharacteristic} className="inline-flex items-center">
          <PlusIcon className="h-4 w-4 mr-2" aria-hidden="true" />
          New Characteristic
        </Button>
      </div>

      {dashboardError && (
        <ErrorState
          message="Could not load SPC dashboard data."
          onRetry={fetchDashboard}
        />
      )}

      {/* Summary Cards -- field names come straight off GET /spc/dashboard. There is no
          `measurements_today` on that response; the fourth tile shows the below-threshold
          Cpk count, which the endpoint does return. */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={ChartBarIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Characteristics Monitored"
          value={stats?.total_characteristics ?? 0}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Out-of-Control Alerts"
          value={stats?.out_of_control_count ?? 0}
          valueColor={(stats?.out_of_control_count ?? 0) > 0 ? 'text-fd-red' : undefined}
        />
        <MiniStat
          icon={BeakerIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Average Cpk"
          value={fmtFixed(stats?.average_cpk, 2)}
          subtitle="Latest study per characteristic"
        />
        <MiniStat
          icon={ClipboardDocumentCheckIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Below Cpk 1.33"
          value={stats?.characteristics_below_cpk_threshold ?? 0}
          valueColor={(stats?.characteristics_below_cpk_threshold ?? 0) > 0 ? 'text-fd-amber' : undefined}
        />
      </MiniStatStrip>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 items-start">
        {/* Characteristic Selector. The count in the footer is a REAL total: the client
            pages GET /spc/characteristics to completion rather than taking the route's
            silent default of 100 and reporting it as the whole set. */}
        <CockpitPanel
          title="Characteristics"
          subtitle={showInactive ? 'Active and inactive' : 'Active only'}
          className="lg:col-span-1"
          footer={`${characteristics.length} characteristic${characteristics.length === 1 ? '' : 's'}`}
          headerExtra={
            <div className="flex items-center gap-1.5">
              <input
                id="spc-show-inactive"
                type="checkbox"
                checked={showInactive}
                onChange={(e) => setShowInactive(e.target.checked)}
                className="h-3.5 w-3.5 rounded-sm border-slate-600 bg-transparent"
              />
              <label htmlFor="spc-show-inactive" className="text-xs text-slate-400 whitespace-nowrap">
                Show inactive
              </label>
            </div>
          }
        >
          <div className="space-y-1">
            {characteristics.map((c) => (
              <button
                type="button"
                key={c.id}
                onClick={() => setSelectedId(c.id)}
                className={`w-full text-left px-3 py-2 rounded-sm text-sm transition-colors min-w-0 ${
                  selectedId === c.id
                    ? 'bg-fd-blue/20 text-fd-blue font-medium'
                    : 'text-slate-300 hover:bg-fd-raised'
                }`}
              >
                <span className="block truncate">
                  {c.name}
                  {c.is_critical && <span className="ml-1 text-fd-red" title="Critical characteristic">*</span>}
                  {!c.is_active && (
                    <span className="ml-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                      Inactive
                    </span>
                  )}
                </span>
                {/* The route orders by name alone, so the part is what tells two
                    identically-named characteristics apart. */}
                <span className="block text-xs text-slate-500 truncate">
                  {partLabel(c.part_id)} · {SPC_CHART_TYPE_LABELS[c.chart_type] ?? c.chart_type} · n=
                  {c.subgroup_size}
                </span>
              </button>
            ))}
            {characteristics.length === 0 && (
              <EmptyState
                icon={BeakerIcon}
                title="No characteristics"
                description="Define a characteristic to start monitoring it."
                action={{ label: 'New Characteristic', onClick: openCreateCharacteristic }}
                className="px-3 py-8"
              />
            )}
          </div>
        </CockpitPanel>

        {/* Main Content */}
        <div className="lg:col-span-3 space-y-4">
          {selectedChar && detailsError ? (
            <ErrorState
              message={`Could not load details for ${selectedChar.name}.`}
              onRetry={() => fetchCharacteristicDetails(selectedChar.id)}
            />
          ) : selectedChar ? (
            <>
              {/* Control Chart + Process Capability side-by-side */}
              <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
                <CockpitPanel
                  title={`Control Chart: ${selectedChar.name}`}
                  subtitle={
                    controlLimits
                      ? `${partLabel(selectedChar.part_id)} · limits from ${
                          controlLimits.sample_count
                        } subgroup${
                          controlLimits.sample_count === 1 ? '' : 's'
                        }, ${formatCentralDateTime(controlLimits.calculation_date)}`
                      : `${partLabel(selectedChar.part_id)} · no control limits calculated yet`
                  }
                  className="xl:col-span-8"
                  bodyClassName="lg:max-h-none"
                  headerExtra={
                    canRecalculate && !subgroupSizeUnsupported ? (
                      <LoadingButton
                        size="sm"
                        variant="secondary"
                        loading={recalculating}
                        loadingText="Recalculating…"
                        onClick={handleRecalculate}
                      >
                        <ArrowPathIcon className="h-4 w-4 mr-1" aria-hidden="true" />
                        Recalculate
                      </LoadingButton>
                    ) : undefined
                  }
                >
                  {/* The server implements ONE chart model. A characteristic labelled
                      otherwise is still evaluated under X-bar/R rules, so say so rather
                      than letting the label imply a model the system does not run. */}
                  {chartTypeUnsupported && (
                    <p className="mb-3 rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-fd-amber">
                      This characteristic is labelled{' '}
                      {SPC_CHART_TYPE_LABELS[selectedChar.chart_type] ?? selectedChar.chart_type}, but the
                      server computes control limits and Western Electric rules as X-bar/R for every chart
                      type. Read the limits below as X-bar/R.
                    </p>
                  )}

                  {subgroupSizeUnsupported && (
                    <p className="mb-3 rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-fd-amber">
                      Subgroup size {selectedChar.subgroup_size} is outside the {MIN_SUBGROUP_SIZE}–
                      {MAX_SUBGROUP_SIZE} range the control-limit calculator supports, so no limits,
                      violations or capability study can be produced for it. Measurements are still recorded.
                    </p>
                  )}

                  {collapsedLimitsRisk && (
                    <p className="mb-3 rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-fd-amber">
                      This characteristic is fed one sample per subgroup (process-sheet capture) while its
                      declared subgroup size is {selectedChar.subgroup_size}. Recalculating will collapse the
                      X-bar/R control limits (UCL = CL = LCL) and clear recorded violations.
                    </p>
                  )}

                  {chartPoints.length === 0 ? (
                    <EmptyState
                      icon={ChartBarIcon}
                      title="No measurements yet"
                      description="Record a subgroup to start plotting this characteristic."
                      action={{ label: 'Add Measurement', onClick: openMeasurementModal }}
                    />
                  ) : (
                    <>
                      <div className="h-80">
                        <ResponsiveContainer width="100%" height="100%">
                          <LineChart data={chartPoints} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} />
                            <XAxis
                              dataKey="subgroup_number"
                              tick={{ fontSize: 12, fill: CHART_COLORS.axis }}
                              stroke={CHART_COLORS.grid}
                            />
                            <YAxis
                              tick={{ fontSize: 12, fill: CHART_COLORS.axis }}
                              stroke={CHART_COLORS.grid}
                              domain={['auto', 'auto']}
                            />
                            <Tooltip content={<ChartTooltip metric="mean" unit={unit} />} />
                            <Legend />
                            {/* Control limits (voice of the process) -- never conflated with
                                the spec limits below (voice of the customer). */}
                            {controlLimits && (
                              <>
                                <ReferenceLine
                                  y={controlLimits.ucl}
                                  stroke={CHART_COLORS.control}
                                  strokeDasharray="5 5"
                                  label="UCL"
                                />
                                <ReferenceLine
                                  y={controlLimits.center_line}
                                  stroke={CHART_COLORS.center}
                                  strokeDasharray="3 3"
                                  label="CL"
                                />
                                <ReferenceLine
                                  y={controlLimits.lcl}
                                  stroke={CHART_COLORS.control}
                                  strokeDasharray="5 5"
                                  label="LCL"
                                />
                              </>
                            )}
                            {selectedChar.specification_usl !== null && (
                              <ReferenceLine
                                y={selectedChar.specification_usl}
                                stroke={CHART_COLORS.spec}
                                strokeDasharray="10 4"
                                label="USL"
                              />
                            )}
                            {selectedChar.specification_lsl !== null && (
                              <ReferenceLine
                                y={selectedChar.specification_lsl}
                                stroke={CHART_COLORS.spec}
                                strokeDasharray="10 4"
                                label="LSL"
                              />
                            )}
                            <Line
                              type="monotone"
                              dataKey="mean"
                              stroke={CHART_COLORS.series}
                              strokeWidth={2}
                              dot={(props: any) => {
                                const { cx, cy, payload, index } = props;
                                const ooc = Boolean(payload?.is_out_of_control);
                                return (
                                  <circle
                                    key={`xbar-dot-${index}`}
                                    cx={cx}
                                    cy={cy}
                                    r={ooc ? 5 : 3}
                                    fill={ooc ? CHART_COLORS.control : CHART_COLORS.series}
                                    stroke={ooc ? '#fecaca' : 'none'}
                                    strokeWidth={ooc ? 1.5 : 0}
                                  />
                                );
                              }}
                              activeDot={{ r: 5 }}
                              name="Subgroup mean (X̄)"
                            />
                          </LineChart>
                        </ResponsiveContainer>
                      </div>

                      {!controlLimits && (
                        <p className="mt-2 text-xs text-slate-400">
                          No control limits calculated yet — the chart shows subgroup means without UCL/CL/LCL.
                        </p>
                      )}

                      {/* R chart -- only meaningful when some subgroup holds more than one sample. */}
                      {showRangeChart && (
                        <div className="mt-4">
                          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
                            Range (R) chart
                          </p>
                          <div className="h-44">
                            <ResponsiveContainer width="100%" height="100%">
                              <LineChart data={chartPoints} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                                <CartesianGrid strokeDasharray="3 3" stroke={CHART_COLORS.grid} />
                                <XAxis
                                  dataKey="subgroup_number"
                                  tick={{ fontSize: 11, fill: CHART_COLORS.axis }}
                                  stroke={CHART_COLORS.grid}
                                />
                                <YAxis
                                  tick={{ fontSize: 11, fill: CHART_COLORS.axis }}
                                  stroke={CHART_COLORS.grid}
                                  domain={['auto', 'auto']}
                                />
                                <Tooltip content={<ChartTooltip metric="range" unit={unit} />} />
                                {controlLimits?.ucl_range !== null && controlLimits?.ucl_range !== undefined && (
                                  <ReferenceLine
                                    y={controlLimits.ucl_range}
                                    stroke={CHART_COLORS.control}
                                    strokeDasharray="5 5"
                                    label="UCLr"
                                  />
                                )}
                                {controlLimits?.center_line_range !== null &&
                                  controlLimits?.center_line_range !== undefined && (
                                    <ReferenceLine
                                      y={controlLimits.center_line_range}
                                      stroke={CHART_COLORS.center}
                                      strokeDasharray="3 3"
                                      label="R̄"
                                    />
                                  )}
                                {controlLimits?.lcl_range !== null && controlLimits?.lcl_range !== undefined && (
                                  <ReferenceLine
                                    y={controlLimits.lcl_range}
                                    stroke={CHART_COLORS.control}
                                    strokeDasharray="5 5"
                                    label="LCLr"
                                  />
                                )}
                                <Line
                                  type="monotone"
                                  dataKey="range"
                                  stroke={CHART_COLORS.range}
                                  strokeWidth={2}
                                  dot={{ r: 2, fill: CHART_COLORS.range }}
                                  name="Subgroup range (R)"
                                />
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        </div>
                      )}
                    </>
                  )}

                  <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-400 tabular-nums">
                    <span>Nominal: {fmt(selectedChar.specification_nominal)}</span>
                    <span>USL: {fmt(selectedChar.specification_usl)}</span>
                    <span>LSL: {fmt(selectedChar.specification_lsl)}</span>
                    {unit && <span>Unit: {unit}</span>}
                    <span className="text-slate-500">Spec limits (customer), not control limits (process)</span>
                  </div>
                </CockpitPanel>

                {/* Process Capability */}
                {capability && (
                  <CockpitPanel
                    title="Process Capability"
                    subtitle={`${capability.sample_count} measurement${
                      capability.sample_count === 1 ? '' : 's'
                    }, ${formatCentralDateTime(capability.study_date)}`}
                    className="xl:col-span-4"
                    bodyClassName="lg:max-h-none"
                  >
                    {/* Only Cpk/Ppk carry a capability verdict. Cp/Pp ignore centring, so a
                        badly off-centre process can show a high Cp and a failing Cpk at
                        once -- and the server's own verdict is `is_capable = cpk >= 1.33`.
                        Colouring a Cp tile green "Capable" would print a claim the quality
                        system does not make, next to "Not Capable" on the Cpk tile. */}
                    <div className="grid grid-cols-2 gap-2">
                      {CAPABILITY_TILES.map((item) => {
                        const value = capability[item.key];
                        const tone = item.tone(value);
                        return (
                          <div
                            key={item.label}
                            className={`border rounded-sm p-3 text-center min-w-0 ${tone.bg}`}
                          >
                            <p className="text-xs font-medium text-slate-400">{item.label}</p>
                            <p className={`text-xl font-bold mt-1 tabular-nums ${tone.text}`}>
                              {fmtFixed(value, 3)}
                            </p>
                            <p className="text-[10px] text-slate-500 mt-1 truncate">{tone.label}</p>
                          </div>
                        );
                      })}
                    </div>
                    <p className="mt-2 text-[10px] text-slate-500">
                      Cp/Pp measure spread only and carry no capability verdict; the capable / not-capable
                      call is Cpk-based, matching the server. Pp/Ppk are computed by the server from the
                      same within-subgroup sigma as Cp/Cpk.
                    </p>
                  </CockpitPanel>
                )}
              </div>

              {/* Recent Measurements + Violations side-by-side */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
                {/* Recent Measurements */}
                {/* No footer count: CockpitPanel renders it as "<footer> total", and this
                    panel deliberately holds a WINDOW, not a total. The route returns no
                    count and this page never fetches the whole history, so any number
                    printed here as a total would be a fabrication. */}
                <CockpitPanel
                  title="Recent Measurements"
                  subtitle={`Newest ${recentMeasurements.length}, from the last ${CHART_WINDOW} subgroups`}
                  headerExtra={
                    <Button size="sm" onClick={openMeasurementModal} className="inline-flex items-center">
                      <PlusIcon className="h-4 w-4 mr-1" aria-hidden="true" />
                      Add Measurement
                    </Button>
                  }
                >
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-fd-line text-sm">
                      <thead className="bg-fd-sunken">
                        <tr>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Subgroup</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Sample</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Value</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Time</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">OOC</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Notes</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-fd-line/40">
                        {recentMeasurements.map((m) => (
                          // The OOC highlight uses the SERVER's stored flag rather than a
                          // client-side value-vs-UCL recomputation, which would duplicate
                          // (and could contradict) the Western Electric evaluation.
                          <tr key={m.id} className={m.is_out_of_control ? 'bg-fd-red/10' : ''}>
                            <td className="px-3 py-2 text-slate-300 tabular-nums">#{m.subgroup_number}</td>
                            <td className="px-3 py-2 text-slate-400 tabular-nums">{m.sample_number}</td>
                            <td
                              className={`px-3 py-2 font-mono tabular-nums ${
                                m.is_out_of_control ? 'text-fd-red font-bold' : ''
                              }`}
                            >
                              {fmt(m.measurement_value)}
                              {unit && <span className="ml-1 text-slate-500">{unit}</span>}
                            </td>
                            <td className="px-3 py-2 text-slate-400 whitespace-nowrap">
                              {formatCentralDateTime(m.measured_at)}
                            </td>
                            <td className="px-3 py-2">
                              {m.is_out_of_control ? (
                                <span className="text-fd-red text-xs font-semibold">
                                  {m.violation_rules || 'OOC'}
                                </span>
                              ) : (
                                <span className="text-slate-600 text-xs">--</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-slate-400 truncate">{m.notes || '--'}</td>
                          </tr>
                        ))}
                        {recentMeasurements.length === 0 && (
                          <tr>
                            <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                              No measurements recorded.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </CockpitPanel>

                {/* Violations */}
                <CockpitPanel
                  title="Control Violations"
                  subtitle={
                    violationsResponse?.total_subgroups !== undefined
                      ? `Evaluated live against the current control limits · ${
                          violationsResponse.total_subgroups
                        } subgroup${violationsResponse.total_subgroups === 1 ? '' : 's'} checked`
                      : 'Evaluated live against the current control limits'
                  }
                  className={violations.length > 0 ? 'border-fd-red/30' : undefined}
                  footer={
                    violationsResponse?.total_subgroups !== undefined
                      ? `${violations.length} violating subgroup${violations.length === 1 ? '' : 's'}`
                      : undefined
                  }
                  headerExtra={
                    violations.length > 0 ? (
                      <ExclamationTriangleIcon className="h-5 w-5 text-fd-red" aria-hidden="true" />
                    ) : undefined
                  }
                >
                  {hasNoControlLimitsYet || violationsResponse?.message ? (
                    <EmptyState
                      icon={ChartBarIcon}
                      title="No control limits calculated yet"
                      description={
                        violationsResponse?.message ||
                        'Western Electric rules can only be evaluated once control limits exist.'
                      }
                      action={
                        canRecalculate && !subgroupSizeUnsupported
                          ? { label: 'Recalculate control limits', onClick: handleRecalculate }
                          : undefined
                      }
                    />
                  ) : violations.length === 0 ? (
                    <EmptyState
                      icon={ClipboardDocumentCheckIcon}
                      title="No violations"
                      description="Every subgroup passes the Western Electric rules against the current limits."
                    />
                  ) : (
                    <div className="space-y-2">
                      {violations.map((v, idx) => {
                        const point =
                          v.subgroup_number !== null ? chartPointBySubgroup.get(v.subgroup_number) : undefined;
                        return (
                          <div
                            key={v.subgroup_number ?? `violation-${idx}`}
                            className="flex items-start gap-3 p-3 bg-fd-red/10 rounded-sm min-w-0"
                          >
                            <ExclamationTriangleIcon
                              className="h-5 w-5 text-fd-red mt-0.5 flex-shrink-0"
                              aria-hidden="true"
                            />
                            <div className="min-w-0">
                              <p className="text-sm font-medium text-red-300 tabular-nums">
                                Subgroup #{v.subgroup_number ?? '--'} · mean {fmt(v.subgroup_mean)}
                                {unit ? ` ${unit}` : ''}
                              </p>
                              <div className="mt-1 flex flex-wrap gap-1">
                                {v.rules_violated.map((rule) => (
                                  <span
                                    key={rule}
                                    title={SPC_VIOLATION_RULE_LABELS[rule] || rule}
                                    className="inline-flex items-center rounded-sm border border-fd-red/40 bg-fd-red/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-fd-red"
                                  >
                                    {rule}
                                  </span>
                                ))}
                              </div>
                              {/* No timestamp on the violations payload -- joined from the
                                  chart window, omitted rather than invented when outside it. */}
                              {point?.measured_at && (
                                <p className="text-xs text-red-400 mt-1">
                                  {formatCentralDateTime(point.measured_at)}
                                </p>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </CockpitPanel>
              </div>
            </>
          ) : (
            <EmptyState
              icon={ChartBarIcon}
              title="Select a characteristic to view control charts"
              description="Choose from the list on the left or create a new one."
              action={{ label: 'New Characteristic', onClick: openCreateCharacteristic }}
            />
          )}
        </div>
      </div>

      {/* Out-of-Control Alerts */}
      {outOfControl.length > 0 && (
        <CockpitPanel
          title={`Out-of-Control Alerts (${outOfControl.length})`}
          className="border-fd-red/30"
          footer={`${outOfControl.length} alert${outOfControl.length === 1 ? '' : 's'}`}
          headerExtra={<ExclamationTriangleIcon className="h-5 w-5 text-fd-red" aria-hidden="true" />}
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {outOfControl.map((alert) => (
              <button
                type="button"
                key={alert.characteristic_id}
                onClick={() => setSelectedId(alert.characteristic_id)}
                className="text-left w-full p-3 bg-fd-red/10 rounded-sm border border-fd-red/30 cursor-pointer hover:bg-fd-red/20 transition-colors min-w-0"
              >
                <p className="text-sm font-medium text-red-300 truncate">
                  {alert.characteristic_name}
                  {alert.is_critical && (
                    <span className="ml-2 inline-flex items-center rounded-sm border border-fd-red/50 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-fd-red">
                      Critical
                    </span>
                  )}
                </p>
                <p className="text-[10px] text-slate-400 truncate">{partLabel(alert.part_id)}</p>
                <p className="text-xs text-fd-red mt-1 tabular-nums">
                  {alert.ooc_count} out-of-control measurement{alert.ooc_count === 1 ? '' : 's'}
                </p>
                <p className="text-[10px] text-red-400 mt-0.5">
                  Last: {formatCentralDateTime(alert.last_ooc)}
                </p>
              </button>
            ))}
          </div>
        </CockpitPanel>
      )}

      {/* Add Measurement Modal */}
      <Modal
        open={showAddMeasurement}
        onClose={() => setShowAddMeasurement(false)}
        size="md"
        closeOnBackdrop={false}
        ariaLabelledBy="spc-add-measurement-title"
      >
        <div className="flex items-center justify-between mb-4">
          <h3 id="spc-add-measurement-title" className="text-lg font-semibold text-white">
            Add Measurement
          </h3>
          <button type="button" aria-label="Close" onClick={() => setShowAddMeasurement(false)}>
            <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" aria-hidden="true" />
          </button>
        </div>
        <div className="space-y-4">
          {/* The subgroup number is DERIVED and read-only. A free-text subgroup input would
              let a typed number silently merge new readings into an existing rational
              subgroup, retroactively changing that subgroup's X̄ and R on a quality record.
              The number shown is the one derived at the last refresh; the number actually
              written is re-derived from a fresh read at save, because the kiosk writes to
              the same characteristic concurrently. */}
          <div className="rounded-sm border border-fd-line bg-fd-sunken px-3 py-2 text-xs text-slate-400">
            <p className="text-slate-300">
              Subgroup #{nextSubgroupNumber} <span className="text-slate-500">(next)</span>
            </p>
            <p className="mt-0.5">
              {selectedChar?.name} · one rational subgroup of {measurementForm.samples.length} sample
              {measurementForm.samples.length === 1 ? '' : 's'}
              {unit ? ` · ${unit}` : ''}
            </p>
            <p className="mt-0.5 text-slate-500">
              Recorded against your signed-in user. The final subgroup number is assigned when you save.
            </p>
          </div>

          <div className={measurementForm.samples.length > 1 ? 'grid grid-cols-2 gap-3' : ''}>
            {measurementForm.samples.map((value, idx) => (
              <FormField
                // Samples are positional and fixed-length for the open modal, so the index
                // is a stable key here.
                key={`sample-${idx}`}
                label={measurementForm.samples.length === 1 ? 'Value' : `Sample ${idx + 1}`}
                required
                error={sampleErrors[idx]}
                labelClassName="block text-sm font-medium text-slate-300 mb-1"
              >
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    step="any"
                    value={value}
                    onChange={(e) => {
                      const samples = [...measurementForm.samples];
                      samples[idx] = e.target.value;
                      setMeasurementForm({ ...measurementForm, samples });
                    }}
                    className={sampleErrors[idx] ? 'input-error' : 'input'}
                    placeholder="Measured value"
                  />
                )}
              </FormField>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <FormField label="Lot Number" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  maxLength={MAX_LENGTHS.lot_number}
                  value={measurementForm.lot_number}
                  onChange={(e) => setMeasurementForm({ ...measurementForm, lot_number: e.target.value })}
                  className="input"
                  placeholder="Optional"
                />
              )}
            </FormField>
            <FormField label="Serial Number" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  maxLength={MAX_LENGTHS.serial_number}
                  value={measurementForm.serial_number}
                  onChange={(e) => setMeasurementForm({ ...measurementForm, serial_number: e.target.value })}
                  className="input"
                  placeholder="Optional"
                />
              )}
            </FormField>
          </div>

          <FormField
            label="Notes"
            help="Applied to every sample in this subgroup."
            labelClassName="block text-sm font-medium text-slate-300 mb-1"
          >
            {(field) => (
              <textarea
                {...field}
                value={measurementForm.notes}
                onChange={(e) => setMeasurementForm({ ...measurementForm, notes: e.target.value })}
                className="input"
                rows={2}
                placeholder="Optional notes"
              />
            )}
          </FormField>

          <div className="flex justify-end gap-3 pt-2">
            <Button variant="secondary" onClick={() => setShowAddMeasurement(false)}>
              Cancel
            </Button>
            <LoadingButton loading={savingMeasurement} loadingText="Saving…" onClick={handleAddMeasurement}>
              Save Subgroup
            </LoadingButton>
          </div>
        </div>
      </Modal>

      {/* Create Characteristic Modal */}
      <Modal
        open={showCreateChar}
        onClose={() => setShowCreateChar(false)}
        size="2xl"
        closeOnBackdrop={false}
        ariaLabelledBy="spc-create-char-title"
      >
        <div className="flex items-center justify-between mb-4">
          <h3 id="spc-create-char-title" className="text-lg font-semibold text-white">
            New Characteristic
          </h3>
          <button type="button" aria-label="Close" onClick={() => setShowCreateChar(false)}>
            <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" aria-hidden="true" />
          </button>
        </div>
        <div className="space-y-4">
          {partsError && <ErrorState message="Could not load the parts list." onRetry={fetchParts} />}

          <FormField
            label="Name"
            required
            error={charErrors.name}
            labelClassName="block text-sm font-medium text-slate-300 mb-1"
          >
            {(field) => (
              <input
                {...field}
                type="text"
                required
                maxLength={MAX_LENGTHS.name}
                value={charForm.name}
                onChange={(e) => setCharForm({ ...charForm, name: e.target.value })}
                className={charErrors.name ? 'input-error' : 'input'}
                placeholder="e.g., Bore Diameter"
              />
            )}
          </FormField>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <FormField
              label="Part"
              required
              error={charErrors.part_id}
              help="A characteristic can never be moved to another part — the server has no update path for it."
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <select
                  {...field}
                  required
                  value={charForm.part_id}
                  onChange={(e) => setCharForm({ ...charForm, part_id: e.target.value })}
                  className={charErrors.part_id ? 'input-error' : 'input'}
                >
                  <option value="">Select a part…</option>
                  {partOptions.map((part) => (
                    <option key={part.id} value={part.id}>
                      {part.part_number} — {part.name}
                    </option>
                  ))}
                </select>
              )}
            </FormField>

            {/* `characteristic_type` is unvalidated free text server-side yet is rendered
                verbatim into AS9100D evidence summaries, so this closed select is the only
                control that exists. No "other", no free-text fallback. */}
            <FormField
              label="Characteristic Type"
              required
              error={charErrors.characteristic_type}
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <select
                  {...field}
                  required
                  value={charForm.characteristic_type}
                  onChange={(e) => setCharForm({ ...charForm, characteristic_type: e.target.value })}
                  className={charErrors.characteristic_type ? 'input-error' : 'input'}
                >
                  {SPC_CHARACTERISTIC_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type.charAt(0).toUpperCase() + type.slice(1)}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <FormField
              label="Nominal"
              error={charErrors.specification_nominal}
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <input
                  {...field}
                  type="number"
                  step="any"
                  value={charForm.specification_nominal}
                  onChange={(e) => setCharForm({ ...charForm, specification_nominal: e.target.value })}
                  className={charErrors.specification_nominal ? 'input-error' : 'input'}
                />
              )}
            </FormField>
            <FormField
              label="USL"
              error={charErrors.specification_usl}
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <input
                  {...field}
                  type="number"
                  step="any"
                  value={charForm.specification_usl}
                  onChange={(e) => setCharForm({ ...charForm, specification_usl: e.target.value })}
                  className={charErrors.specification_usl ? 'input-error' : 'input'}
                />
              )}
            </FormField>
            <FormField
              label="LSL"
              error={charErrors.specification_lsl}
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <input
                  {...field}
                  type="number"
                  step="any"
                  value={charForm.specification_lsl}
                  onChange={(e) => setCharForm({ ...charForm, specification_lsl: e.target.value })}
                  className={charErrors.specification_lsl ? 'input-error' : 'input'}
                />
              )}
            </FormField>
            <FormField label="Unit" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  maxLength={MAX_LENGTHS.unit_of_measure}
                  value={charForm.unit_of_measure}
                  onChange={(e) => setCharForm({ ...charForm, unit_of_measure: e.target.value })}
                  className="input"
                  placeholder="in, mm, lb…"
                />
              )}
            </FormField>
          </div>
          <p className="text-xs text-slate-400">
            Both USL and LSL are required before a process-capability study can run.
          </p>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/* Fixed, not chosen. The server computes X-bar/R limits and evaluates X-bar
                Western Electric rules for EVERY chart type, so recording a characteristic
                as "P Chart" or "Individual & MR" would store a model the system never runs
                and then evaluate it as X-bar/R anyway. Offering the choice was the trap:
                individuals data collects one sample per subgroup, which collapses X-bar/R
                limits to UCL = CL = LCL and clears every recorded violation. */}
            <FormField
              label="Chart Type"
              help="X-bar & R is the only model this system computes; other chart types are not offered."
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <select {...field} value={SUPPORTED_CHART_TYPE} disabled className="input">
                  <option value={SUPPORTED_CHART_TYPE}>{SPC_CHART_TYPE_LABELS[SUPPORTED_CHART_TYPE]}</option>
                </select>
              )}
            </FormField>
            <FormField
              label="Subgroup Size"
              required
              error={charErrors.subgroup_size}
              help={`Samples per rational subgroup. Control limits require ${MIN_SUBGROUP_SIZE}–${MAX_SUBGROUP_SIZE}; the entry form collects exactly this many.`}
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {(field) => (
                <input
                  {...field}
                  type="number"
                  min={MIN_SUBGROUP_SIZE}
                  max={MAX_SUBGROUP_SIZE}
                  step={1}
                  required
                  value={charForm.subgroup_size}
                  onChange={(e) => setCharForm({ ...charForm, subgroup_size: e.target.value })}
                  className={charErrors.subgroup_size ? 'input-error' : 'input'}
                />
              )}
            </FormField>
          </div>

          <div className="flex items-center gap-2">
            <input
              id="spc-char-is-critical"
              type="checkbox"
              checked={charForm.is_critical}
              onChange={(e) => setCharForm({ ...charForm, is_critical: e.target.checked })}
              className="h-4 w-4 rounded-sm border-slate-600 bg-transparent"
            />
            <label htmlFor="spc-char-is-critical" className="text-sm text-slate-300">
              Critical characteristic
            </label>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <Button variant="secondary" onClick={() => setShowCreateChar(false)}>
              Cancel
            </Button>
            <LoadingButton loading={savingChar} loadingText="Creating…" onClick={handleCreateCharacteristic}>
              Create
            </LoadingButton>
          </div>
        </div>
      </Modal>
    </div>
  );
};

export default SPC;
