const CENTRAL_TIME_ZONE = 'America/Chicago';

type DateInput = string | number | Date | null | undefined;

const DATE_ONLY_REGEX = /^\d{4}-\d{2}-\d{2}$/;
const DATE_TIME_WITHOUT_ZONE_REGEX =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$/;
const formatterCache = new Map<string, Intl.DateTimeFormat>();

const getFormatter = (options: Intl.DateTimeFormatOptions) => {
  const key = JSON.stringify(options);
  const cached = formatterCache.get(key);
  if (cached) {
    return cached;
  }

  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone: CENTRAL_TIME_ZONE,
    ...options,
  });
  formatterCache.set(key, formatter);
  return formatter;
};

/**
 * Parse a backend timestamp into a Date, treating zone-less strings as UTC
 * (the API serializes naive-UTC datetimes without a 'Z' suffix — native
 * `new Date(...)` would mis-parse those as LOCAL time).
 */
export const toDate = (value: DateInput): Date | null => {
  if (value === null || value === undefined || value === '') {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
  }

  if (typeof value === 'string' && DATE_ONLY_REGEX.test(value)) {
    return new Date(`${value}T12:00:00Z`);
  }

  if (typeof value === 'string' && DATE_TIME_WITHOUT_ZONE_REGEX.test(value)) {
    return new Date(`${value}Z`);
  }

  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
};

const getCentralParts = (value: DateInput) => {
  const date = toDate(value);
  if (!date) {
    return null;
  }

  const parts = getFormatter({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);

  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return {
    year: lookup.year,
    month: lookup.month,
    day: lookup.day,
  };
};

const getNormalizedDateStamp = (value: DateInput) => {
  if (typeof value === 'string' && DATE_ONLY_REGEX.test(value)) {
    return value;
  }

  const parts = getCentralParts(value);
  if (!parts) {
    return '';
  }

  return `${parts.year}-${parts.month}-${parts.day}`;
};

export const formatInCentralTime = (
  value: DateInput,
  options: Intl.DateTimeFormatOptions,
  fallback = '-'
) => {
  const date = toDate(value);
  if (!date) {
    return fallback;
  }

  return getFormatter(options).format(date);
};

export const formatCentralDate = (
  value: DateInput,
  options: Intl.DateTimeFormatOptions = {}
) =>
  formatInCentralTime(
    value,
    {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      ...options,
    }
  );

export const formatCentralDateTime = (
  value: DateInput,
  options: Intl.DateTimeFormatOptions = {}
) =>
  formatInCentralTime(
    value,
    {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
      ...options,
    }
  );

export const formatCentralTime = (
  value: DateInput,
  options: Intl.DateTimeFormatOptions = {}
) =>
  formatInCentralTime(
    value,
    {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
      ...options,
    }
  );

/**
 * Minutes elapsed since midnight in Central time (0–1439), e.g. 5:30 AM → 330.
 * Returns NaN for an unparseable value. Used for time-of-day comparisons such
 * as shift detection that must key off the shop's local (Central) wall clock
 * regardless of the viewer's browser timezone.
 */
export const getCentralMinutesOfDay = (value: DateInput = new Date()): number => {
  const date = toDate(value);
  if (!date) {
    return Number.NaN;
  }

  const parts = getFormatter({
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date);

  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  // en-US with hour12:false can emit '24' for midnight — normalize to 0.
  const hour = parseInt(lookup.hour, 10) % 24;
  const minute = parseInt(lookup.minute, 10);
  if (Number.isNaN(hour) || Number.isNaN(minute)) {
    return Number.NaN;
  }
  return hour * 60 + minute;
};

const DATE_TIME_LOCAL_REGEX = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/**
 * The Central-time UTC offset (in ms; negative — Central is behind UTC) that is
 * in effect at a given UTC instant. Reads the zone's wall clock for the instant
 * via Intl and diffs it against the instant, so DST (CST −6 / CDT −5) is handled
 * by the platform rather than hard-coded.
 */
const getCentralOffsetMs = (utcMs: number): number => {
  const parts = getFormatter({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(new Date(utcMs));

  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const asUtc = Date.UTC(
    parseInt(lookup.year, 10),
    parseInt(lookup.month, 10) - 1,
    parseInt(lookup.day, 10),
    // en-US with hour12:false can emit '24' for midnight — normalize to 0.
    parseInt(lookup.hour, 10) % 24,
    parseInt(lookup.minute, 10),
    parseInt(lookup.second, 10)
  );
  return asUtc - utcMs;
};

/**
 * Convert a naive shop-local (Central) wall-clock value from an
 * `<input type="datetime-local">` ("YYYY-MM-DDTHH:mm", no zone) into a UTC
 * ISO-8601 string with a trailing 'Z', ready to send to the API.
 *
 * A datetime-local input is timezone-agnostic; the shop operates in Central, so
 * the entered wall clock is interpreted as America/Chicago and resolved to the
 * correct UTC instant — honoring whichever offset (CST −6 / CDT −5) applies on
 * that date, regardless of the viewer's browser timezone. Returns null for an
 * empty or unparseable input.
 */
export const centralWallClockToUtcISO = (value: string | null | undefined): string | null => {
  if (!value) {
    return null;
  }
  const match = DATE_TIME_LOCAL_REGEX.exec(value.trim());
  if (!match) {
    return null;
  }
  const [, year, month, day, hour, minute, second] = match;

  // Treat the wall clock as if it were UTC to get a first-guess instant, then
  // subtract the Central offset in effect at that instant. One correction pass
  // handles the rare case where the guess and the true instant straddle a DST
  // transition and would otherwise resolve different offsets.
  const wallAsUtcMs = Date.UTC(
    parseInt(year, 10),
    parseInt(month, 10) - 1,
    parseInt(day, 10),
    parseInt(hour, 10),
    parseInt(minute, 10),
    second ? parseInt(second, 10) : 0
  );
  if (Number.isNaN(wallAsUtcMs)) {
    return null;
  }
  let instant = wallAsUtcMs - getCentralOffsetMs(wallAsUtcMs);
  instant = wallAsUtcMs - getCentralOffsetMs(instant);

  const result = new Date(instant);
  return Number.isNaN(result.getTime()) ? null : result.toISOString();
};

/**
 * The current shop-local (Central) wall clock as a "YYYY-MM-DDTHH:mm" string,
 * for seeding or bounding an `<input type="datetime-local">` (e.g. its `max`).
 * Reflects Central regardless of the viewer's browser timezone.
 */
export const getCentralNowDateTimeLocal = (value: DateInput = new Date()): string => {
  const date = toDate(value);
  if (!date) {
    return '';
  }
  const parts = getFormatter({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date);

  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const hour = String(parseInt(lookup.hour, 10) % 24).padStart(2, '0');
  return `${lookup.year}-${lookup.month}-${lookup.day}T${hour}:${lookup.minute}`;
};

export const getCentralDateStamp = (value: DateInput = new Date()) => getNormalizedDateStamp(value);

export const getCentralTodayISODate = () => getCentralDateStamp(new Date());

export const getCentralTodayDate = () => toDate(getCentralTodayISODate()) ?? new Date();

export const isDateTodayInCentral = (value: DateInput) =>
  getNormalizedDateStamp(value) === getCentralTodayISODate();

export const isDateBeforeTodayInCentral = (value: DateInput) => {
  const normalized = getNormalizedDateStamp(value);
  return Boolean(normalized) && normalized < getCentralTodayISODate();
};

export const getDateSortValue = (value: DateInput) => {
  const normalized = getNormalizedDateStamp(value);
  return normalized ? Date.parse(`${normalized}T00:00:00Z`) : Number.MAX_SAFE_INTEGER;
};

export const toCentralCalendarDate = (value: DateInput) => toDate(value);
