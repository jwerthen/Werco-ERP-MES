const CENTRAL_TIME_ZONE = 'America/Chicago';

type DateInput = string | number | Date | null | undefined;

const DATE_ONLY_REGEX = /^\d{4}-\d{2}-\d{2}$/;
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

const toDate = (value: DateInput): Date | null => {
  if (value === null || value === undefined || value === '') {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
  }

  if (typeof value === 'string' && DATE_ONLY_REGEX.test(value)) {
    return new Date(`${value}T12:00:00Z`);
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
