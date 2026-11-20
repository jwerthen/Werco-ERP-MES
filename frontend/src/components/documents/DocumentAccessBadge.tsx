import React from 'react';
import { LockClosedIcon, ShieldExclamationIcon } from '@heroicons/react/24/outline';
import {
  DOCUMENT_ACCESS_DESCRIPTIONS,
  DOCUMENT_ACCESS_LABELS,
  type DocumentAccessLevel,
} from '../../types/documentAccess';

const CHIP =
  'inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-px font-mono text-[10px] font-semibold uppercase tracking-wide';

/** Chip classes per level: standard is quiet, restricted amber, export-controlled red. */
export const DOCUMENT_ACCESS_CHIP_CLASS: Readonly<Record<DocumentAccessLevel, string>> = {
  standard: `${CHIP} border-fd-line text-fd-mute`,
  restricted: `${CHIP} border-fd-amber/50 bg-fd-amber/10 text-fd-amber`,
  export_controlled: `${CHIP} border-fd-red/50 bg-fd-red/10 text-fd-red`,
};

interface DocumentAccessBadgeProps {
  level: DocumentAccessLevel;
  /**
   * Render a quiet chip for `standard` too (a table column, a record header). Off by default:
   * most surfaces mark only the documents that are NOT open to everyone.
   */
  showStandard?: boolean;
  className?: string;
}

/**
 * A document's access level as a chip. Restricted and export-controlled always render, with an
 * icon and the level's audience as the tooltip, so a flagged document reads as flagged at a
 * glance; standard renders only on request.
 */
export function DocumentAccessBadge({ level, showStandard = false, className = '' }: DocumentAccessBadgeProps) {
  if (level === 'standard' && !showStandard) return null;
  const Icon = level === 'export_controlled' ? ShieldExclamationIcon : level === 'restricted' ? LockClosedIcon : null;
  return (
    <span
      className={`${DOCUMENT_ACCESS_CHIP_CLASS[level]} ${className}`.trim()}
      title={DOCUMENT_ACCESS_DESCRIPTIONS[level]}
      data-testid="document-access-badge"
      data-access-level={level}
    >
      {Icon && <Icon className="h-3 w-3" aria-hidden="true" />}
      {DOCUMENT_ACCESS_LABELS[level]}
    </span>
  );
}

export default DocumentAccessBadge;
