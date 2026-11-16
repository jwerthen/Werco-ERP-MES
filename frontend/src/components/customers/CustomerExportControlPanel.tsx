/**
 * Customers → customer detail → Export control (ADMIN only).
 *
 * Shows whether the customer is EXPORT-CONTROLLED — derived by the server from
 * every contract it has ever had (any active flag, any dates) and from classified
 * handling instructions, the exact rule AI-external API tokens are redacted by —
 * and lists its contracts, with add / edit. There is no delete: un-marking is an
 * edit that clears the flags, with a reason.
 *
 * RBAC: every endpoint behind this panel is `require_role([ADMIN])` and refuses
 * an API-token credential, so the caller renders the panel only for the same
 * role set (admin, platform admin, superuser). Writes are server-gated and
 * audited fail-closed, so they are non-optimistic: the panel shows the view the
 * server returns after each save, never a guess.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { PencilIcon, PlusIcon, ShieldExclamationIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, ErrorState, useToast } from '../ui';
import type { DataTableColumn } from '../ui';
import CustomerContractDialog from './CustomerContractDialog';
import { dataClassificationLabel, type CustomerContract, type CustomerExportControl } from '../../types/exportControl';
import { formatCentralDate } from '../../utils/centralTime';

const CHIP = 'inline-block rounded-sm border px-1.5 py-px font-mono text-[10px] font-semibold uppercase tracking-wide';
const CONTROL_CHIP = `${CHIP} border-fd-red/50 bg-fd-red/10 text-fd-red`;
const QUIET_CHIP = `${CHIP} border-fd-line text-fd-mute`;

interface CustomerExportControlPanelProps {
  customerId: number;
  customerName: string;
}

type DialogState = { mode: 'closed' } | { mode: 'add' } | { mode: 'edit'; contract: CustomerContract };

function contractFlags(contract: CustomerContract): string[] {
  const flags: string[] = [];
  if (contract.itar_controlled) flags.push('ITAR');
  if (contract.export_controlled) flags.push('Export');
  if (contract.contains_cui) flags.push('CUI');
  return flags;
}

export default function CustomerExportControlPanel({ customerId, customerName }: CustomerExportControlPanelProps) {
  const { showToast } = useToast();
  const [view, setView] = useState<CustomerExportControl | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [dialog, setDialog] = useState<DialogState>({ mode: 'closed' });

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      setView(await api.getCustomerExportControl(customerId));
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [customerId]);

  useEffect(() => {
    setView(null);
    load();
  }, [load]);

  const handleSaved = (next: CustomerExportControl) => {
    setView(next);
    setDialog({ mode: 'closed' });
    showToast(
      'success',
      next.controlled
        ? `Contract saved. ${next.customer_name} is export-controlled (${dataClassificationLabel(next.data_classification)}).`
        : `Contract saved. ${next.customer_name} is not export-controlled.`
    );
  };

  const columns = useMemo<Array<DataTableColumn<CustomerContract>>>(
    () => [
      {
        key: 'contract',
        header: 'Contract',
        accessor: row => row.contract_name,
        render: row => (
          <div className="min-w-0">
            <div className="font-medium text-fd-ink [overflow-wrap:anywhere]">{row.contract_name}</div>
            {row.contract_number && <div className="font-mono text-[11px] text-fd-mute">{row.contract_number}</div>}
          </div>
        ),
      },
      {
        key: 'flags',
        header: 'Flags',
        accessor: row => contractFlags(row).join(' '),
        render: row => {
          const flags = contractFlags(row);
          return flags.length ? (
            <div className="flex flex-wrap gap-1">
              {flags.map(flag => (
                <span key={flag} className={CONTROL_CHIP}>
                  {flag}
                </span>
              ))}
            </div>
          ) : (
            <span className="text-fd-faint">—</span>
          );
        },
      },
      {
        key: 'classification',
        header: 'Classification',
        accessor: row => dataClassificationLabel(row.default_data_classification),
      },
      {
        key: 'dates',
        header: 'Effective – Expires',
        accessor: row => row.effective_date ?? '',
        render: row => (
          <span className="font-mono text-xs">
            {row.effective_date ? formatCentralDate(row.effective_date) : '—'} –{' '}
            {row.expiration_date ? formatCentralDate(row.expiration_date) : '—'}
          </span>
        ),
      },
      {
        key: 'active',
        header: 'Active',
        accessor: row => (row.active ? 'yes' : 'no'),
        render: row => <span className={QUIET_CHIP}>{row.active ? 'Active' : 'Inactive'}</span>,
      },
      {
        key: 'controlling',
        header: 'Controls',
        accessor: row => (row.controlling ? 'yes' : 'no'),
        render: row =>
          row.controlling ? <span className={CONTROL_CHIP}>Controls</span> : <span className="text-fd-faint">—</span>,
      },
      {
        key: 'actions',
        header: <span className="sr-only">Actions</span>,
        align: 'right',
        render: row => (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Edit contract ${row.contract_name}`}
            onClick={() => setDialog({ mode: 'edit', contract: row })}
          >
            <PencilIcon className="h-4 w-4" aria-hidden="true" />
          </Button>
        ),
      },
    ],
    []
  );

  const controlledBy =
    view?.controlled && view.controlling_contract_id === null
      ? 'by a classified handling instruction'
      : view?.controlled
        ? `by contract ${
            view.contracts.find(contract => contract.id === view.controlling_contract_id)?.contract_name ?? ''
          }`.trim()
        : '';

  return (
    <section
      className="rounded-sm border border-fd-line bg-fd-panel p-3"
      aria-labelledby={`export-control-${customerId}`}
      data-testid="export-control-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex items-start gap-2">
          <ShieldExclamationIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-blue" aria-hidden="true" />
          <div>
            <h3 id={`export-control-${customerId}`} className="text-sm font-semibold text-fd-ink">
              Export control
            </h3>
            <p className="text-[11px] text-fd-mute">Admin only. Every change needs a reason and is audited.</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {view && (
            <span className={view.controlled ? CONTROL_CHIP : QUIET_CHIP} data-testid="export-control-badge">
              {view.controlled ? `Controlled · ${dataClassificationLabel(view.data_classification)}` : 'Not controlled'}
            </span>
          )}
          <Button size="sm" variant="secondary" onClick={() => setDialog({ mode: 'add' })} disabled={!view}>
            <PlusIcon className="mr-1 h-4 w-4" aria-hidden="true" />
            Add contract
          </Button>
        </div>
      </div>

      {view && (
        <p className="mt-2 text-xs text-fd-body">
          {view.controlled
            ? `Export-controlled ${controlledBy}. AI-external API tokens see this customer's work orders, parts, NCRs, quotes and purchasing records redacted, and each redaction is logged.`
            : "Not export-controlled. AI-external API tokens see this customer's records in full."}
          {view.customer_is_deleted && ' This customer is deleted; its historical records keep this standing.'}
        </p>
      )}

      <div className="mt-3">
        {loadError && !view ? (
          <ErrorState message="Could not load this customer's export control." onRetry={load} />
        ) : (
          <DataTable<CustomerContract>
            columns={columns}
            data={view?.contracts ?? []}
            rowKey={row => row.id}
            loading={loading && !view}
            dense
            empty={{
              title: 'No contracts recorded',
              description: 'Add a contract to mark this customer ITAR, export controlled or CUI.',
            }}
          />
        )}
      </div>

      <CustomerContractDialog
        open={dialog.mode !== 'closed'}
        customerId={customerId}
        customerName={view?.customer_name ?? customerName}
        contract={dialog.mode === 'edit' ? dialog.contract : null}
        onCancel={() => setDialog({ mode: 'closed' })}
        onSaved={handleSaved}
      />
    </section>
  );
}
