import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowPathIcon, CheckCircleIcon, ShieldExclamationIcon, XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, MobileDataCard, type DataTableColumn } from '../ui';
import {
  DOCUMENT_ACCESS_LABELS,
  type DocumentAccessReview,
  type DocumentAccessReviewRow,
} from '../../types/documentAccess';
import { DocumentAccessBadge } from './DocumentAccessBadge';
import { dataClassificationLabel } from '../../types/exportControl';

interface DocumentAccessReviewPanelProps {
  /** Open one document (its detail carries Change access). */
  onOpenDocument: (id: number) => void;
  onClose: () => void;
  /** Bump to re-read the review, e.g. after a level changed. */
  reloadKey?: number;
}

const documentLabel = (row: DocumentAccessReviewRow) =>
  row.revision ? `${row.document_number} Rev ${row.revision}` : row.document_number;

/**
 * Documents → Access review (Admin / Quality; the caller gates it). Lists every document whose
 * linked part's customer is export-controlled but whose own level is not, each with a link that
 * opens the document, where Change access raises it. Documents with no part link cannot be
 * judged by the review, so it counts them instead of guessing. Pure read: nothing here changes
 * a level.
 */
export default function DocumentAccessReviewPanel({
  onOpenDocument,
  onClose,
  reloadKey = 0,
}: DocumentAccessReviewPanelProps) {
  const [review, setReview] = useState<DocumentAccessReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | false>(false);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    setLoading(true);
    setLoadError(false);
    try {
      const next = await api.getDocumentAccessReview();
      if (request === requestRef.current) setReview(next);
    } catch {
      if (request === requestRef.current) setLoadError('Could not load the access review.');
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // A late answer from an unmounted panel must not land anywhere.
  useEffect(
    () => () => {
      requestRef.current += 1;
    },
    []
  );

  const columns = useMemo<Array<DataTableColumn<DocumentAccessReviewRow>>>(
    () => [
      {
        key: 'document',
        header: 'Document',
        sortable: true,
        accessor: row => row.document_number,
        csv: row => `${documentLabel(row)} ${row.title}`.trim(),
        render: row => (
          <div className="min-w-0 max-w-xs">
            <button
              type="button"
              className="text-left font-medium text-fd-blue underline-offset-2 hover:underline"
              onClick={event => {
                event.stopPropagation();
                onOpenDocument(row.id);
              }}
              aria-label={`Open ${documentLabel(row)}`}
            >
              {documentLabel(row)}
            </button>
            <div className="text-xs text-slate-400 [overflow-wrap:anywhere]">{row.title}</div>
          </div>
        ),
      },
      {
        key: 'part',
        header: 'Part',
        sortable: true,
        accessor: row => row.part_number ?? '',
        render: row => <span className="font-mono text-sm font-semibold">{row.part_number || '—'}</span>,
      },
      {
        key: 'customer',
        header: 'Customer',
        sortable: true,
        accessor: row => row.customer_name ?? '',
        csv: row =>
          [row.customer_name, row.classification && dataClassificationLabel(row.classification)]
            .filter(Boolean)
            .join(' · '),
        render: row => (
          <div className="text-sm">
            {row.customer_name || '—'}
            {row.classification && (
              <div className="text-xs text-fd-mute">{dataClassificationLabel(row.classification)}</div>
            )}
          </div>
        ),
      },
      {
        key: 'level',
        header: 'Current level',
        sortable: true,
        accessor: row => DOCUMENT_ACCESS_LABELS[row.access_level],
        render: row => <DocumentAccessBadge level={row.access_level} showStandard />,
      },
    ],
    [onOpenDocument]
  );

  const count = review?.documents.length ?? 0;
  const unlinked = review?.unlinked_count ?? 0;

  return (
    <section
      aria-labelledby="document-access-review-title"
      className="rounded-sm border border-fd-line bg-fd-panel p-3"
      data-testid="document-access-review"
    >
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <ShieldExclamationIcon className="mt-0.5 h-5 w-5 shrink-0 text-fd-red" aria-hidden="true" />
          <div className="min-w-0">
            <h2 id="document-access-review-title" className="text-sm font-semibold text-fd-ink">
              Access review
            </h2>
            <p className="text-xs text-fd-mute">
              Documents whose part belongs to an export-controlled customer but are not marked{' '}
              {DOCUMENT_ACCESS_LABELS.export_controlled}. Open one to change its access.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => void load()} disabled={loading}>
            <ArrowPathIcon className="mr-1 h-4 w-4" aria-hidden="true" />
            Refresh
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close access review">
            <XMarkIcon className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </div>

      {/* An empty review says so in the table's own empty state below. */}
      {review && !loadError && count > 0 && (
        <p role="status" className="mb-2 text-xs text-fd-body">
          {`${count} document${count === 1 ? '' : 's'} to review.`}
        </p>
      )}

      <DataTable<DocumentAccessReviewRow>
        columns={columns}
        data={review?.documents ?? []}
        rowKey={row => row.id}
        onRowClick={row => onOpenDocument(row.id)}
        defaultSort={{ key: 'document', dir: 'asc' }}
        pageSize={10}
        dense
        loading={loading && !review}
        error={loadError}
        onRetry={() => void load()}
        empty={{
          icon: CheckCircleIcon,
          title: 'No documents need review',
          description: `Every document linked to an export-controlled customer is already ${DOCUMENT_ACCESS_LABELS.export_controlled}.`,
        }}
        mobileCards={row => (
          <MobileDataCard
            title={documentLabel(row)}
            subtitle={row.title}
            onClick={() => onOpenDocument(row.id)}
            badge={<DocumentAccessBadge level={row.access_level} showStandard />}
            fields={[
              { label: 'Part', value: row.part_number || '—' },
              {
                label: 'Customer',
                value: [row.customer_name || '—', row.classification && dataClassificationLabel(row.classification)]
                  .filter(Boolean)
                  .join(' · '),
              },
            ]}
          />
        )}
      />

      {review && !loadError && unlinked > 0 && (
        <p className="mt-2 text-xs text-fd-mute">
          {unlinked} document{unlinked === 1 ? ' has' : 's have'} no part link, so the review cannot judge{' '}
          {unlinked === 1 ? 'it' : 'them'}. Check {unlinked === 1 ? 'it' : 'those'} by hand.
        </p>
      )}
    </section>
  );
}
