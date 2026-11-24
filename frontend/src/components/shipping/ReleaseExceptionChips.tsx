import React from 'react';
import { variantClass, StatusVariant } from '../../utils/statusColors';
import type { ReleaseException, ReleaseExceptionCode } from '../../types/shipping';

/**
 * Open quality items on a job (GET /shipping/ready-to-ship and GET /shipping/{id}
 * ``release_exceptions``), as chips. Read only: the chip says what is open, the
 * server's message says which record. Colors come from the central status variants
 * so a chip matches every other badge in the app.
 */
export const RELEASE_EXCEPTION_LABEL: Record<ReleaseExceptionCode, string> = {
  open_ncr: 'Open NCR',
  fai_not_passed: 'First article not passed',
  open_blocker: 'Quality hold',
};

const RELEASE_EXCEPTION_VARIANT: Record<ReleaseExceptionCode, StatusVariant> = {
  open_ncr: 'red',
  fai_not_passed: 'amber',
  open_blocker: 'red',
};

export function releaseExceptionLabel(exception: ReleaseException): string {
  return RELEASE_EXCEPTION_LABEL[exception.code] ?? exception.code.replace(/_/g, ' ');
}

export function ReleaseExceptionChip({ exception }: { exception: ReleaseException }) {
  const variant = RELEASE_EXCEPTION_VARIANT[exception.code] ?? 'slate';
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium whitespace-nowrap ${variantClass[variant]}`}
      title={exception.message}
    >
      {releaseExceptionLabel(exception)}
      <span className="sr-only">: {exception.message}</span>
    </span>
  );
}

interface ReleaseExceptionChipsProps {
  exceptions?: ReleaseException[] | null;
  /** Rendered when nothing is open. */
  emptyLabel?: React.ReactNode;
}

export default function ReleaseExceptionChips({ exceptions, emptyLabel = null }: ReleaseExceptionChipsProps) {
  if (!exceptions || exceptions.length === 0) return <>{emptyLabel}</>;
  return (
    <ul className="flex flex-wrap gap-1" aria-label="Open quality items">
      {exceptions.map((exception, index) => (
        <li key={`${exception.reference_type}-${exception.reference_id ?? index}-${exception.code}`}>
          <ReleaseExceptionChip exception={exception} />
        </li>
      ))}
    </ul>
  );
}

/** The full list, with the server's message for each item (Ship / Issue dialogs). */
export function ReleaseExceptionList({ exceptions }: { exceptions: ReleaseException[] }) {
  return (
    <ul className="divide-y divide-fd-line border border-fd-line rounded-sm" aria-label="Open quality items">
      {exceptions.map((exception, index) => (
        <li
          key={`${exception.reference_type}-${exception.reference_id ?? index}-${exception.code}`}
          className="flex flex-col gap-1 px-3 py-2 sm:flex-row sm:items-start sm:gap-3"
        >
          <span className="shrink-0">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium whitespace-nowrap ${
                variantClass[RELEASE_EXCEPTION_VARIANT[exception.code] ?? 'slate']
              }`}
            >
              {releaseExceptionLabel(exception)}
            </span>
          </span>
          <span className="text-sm text-fd-ink break-words">{exception.message}</span>
        </li>
      ))}
    </ul>
  );
}
