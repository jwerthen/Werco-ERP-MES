import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowUpTrayIcon, ShieldCheckIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { Button } from '../ui/Button';
import { DataTable, DataTableColumn } from '../ui/DataTable';
import { MobileDataCard } from '../ui/MobileDataCard';
import { formatCentralDate } from '../../utils/centralTime';
import type { AslVendor, VendorReevaluationDue } from '../../types/supplierApproval';
import {
  aslStatusLabel,
  basisLabels,
  canWriteAsl,
  isReevaluationOverdue,
  scopeLabels,
} from '../../utils/supplierApproval';
import { AslStatusPill } from './AslStatusPill';
import { UpdateApprovalDialog } from './UpdateApprovalDialog';
import { AslImportWizard } from './AslImportWizard';

/** Which slice of the vendor book the ASL tab shows. Kept in the URL (`?asl=`) by the page. */
export type AslFilter = 'all' | 'on' | 'off' | 'overdue';

export const ASL_FILTERS: AslFilter[] = ['all', 'on', 'off', 'overdue'];

interface Props {
  /** Vendor-name / code search from the page's search box. */
  search: string;
  filter: AslFilter;
  onFilterChange: (filter: AslFilter) => void;
}

type Row = AslVendor & { days_overdue?: number };

const dateText = (value?: string | null) => (value ? formatCentralDate(value) : '—');

/** A vendor switched off (is_active = false): still on the ASL record, not buyable. */
function InactiveTag() {
  return (
    <span className="ml-2 rounded-sm border border-slate-600 px-1.5 py-0.5 align-middle text-[10px] font-medium uppercase tracking-wide text-slate-400">
      Inactive
    </span>
  );
}

/** Re-evaluation due date, red once past. Overdue rows carry the server's day count. */
function reevalCell(row: Row) {
  if (!row.reevaluation_due) return <span className="text-slate-500">—</span>;
  const late = row.days_overdue !== undefined ? row.days_overdue > 0 : isReevaluationOverdue(row);
  return (
    <span className={late ? 'font-medium text-red-400' : 'text-slate-300'}>
      {formatCentralDate(row.reevaluation_due)}
      {late && row.days_overdue !== undefined && <span className="ml-1 text-xs">({row.days_overdue} d overdue)</span>}
      {late && row.days_overdue === undefined && <span className="sr-only"> (overdue)</span>}
    </span>
  );
}

/**
 * Supplier Scorecards → Approved Supplier List, read straight off the VENDOR record
 * (migration 123). The legacy `approved_supplier_list` table is read-only history and is
 * no longer read here.
 *
 * "Re-evaluation overdue" is backed by GET /purchasing/vendors/reevaluation-due, so the
 * chip's count and its rows are the server's own answer (Central today, statuses that owe
 * a re-evaluation), never a client-side guess.
 *
 * Both reads include vendors switched off (`is_active = false`), marked Inactive: the ASL
 * import and Update approval both write to them, and a Disqualified supplier is often also
 * switched off -- hiding them would make the controlled list silently incomplete and leave
 * their approval decisions with no screen to make them on.
 */
export function ApprovedSupplierListPanel({ search, filter, onFilterChange }: Props) {
  const { user } = useAuth();
  const canWrite = canWriteAsl(user);
  const [vendors, setVendors] = useState<AslVendor[]>([]);
  const [overdue, setOverdue] = useState<VendorReevaluationDue[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [editing, setEditing] = useState<AslVendor | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [vendorRows, overdueRows] = await Promise.all([
        api.getVendors({ active_only: false }),
        api.getVendorsReevaluationDue({ include_inactive: true }),
      ]);
      setVendors(Array.isArray(vendorRows) ? vendorRows : []);
      setOverdue(Array.isArray(overdueRows) ? overdueRows : []);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(
    () => ({
      all: vendors.length,
      on: vendors.filter(vendor => !!vendor.approval_status).length,
      off: vendors.filter(vendor => !vendor.approval_status).length,
      overdue: overdue.length,
    }),
    [vendors, overdue]
  );

  const rows = useMemo<Row[]>(() => {
    const base: Row[] =
      filter === 'overdue'
        ? overdue
        : filter === 'on'
          ? vendors.filter(vendor => !!vendor.approval_status)
          : filter === 'off'
            ? vendors.filter(vendor => !vendor.approval_status)
            : vendors;
    const needle = search.trim().toLowerCase();
    if (!needle) return base;
    return base.filter(
      vendor => vendor.name?.toLowerCase().includes(needle) || vendor.code?.toLowerCase().includes(needle)
    );
  }, [filter, overdue, vendors, search]);

  const handleSaved = (saved: AslVendor) => {
    setEditing(null);
    // Reflect only what the server stored, then re-read the overdue list (its membership
    // follows the status and due date that just changed).
    setVendors(prev => prev.map(vendor => (vendor.id === saved.id ? { ...vendor, ...saved } : vendor)));
    void api
      .getVendorsReevaluationDue({ include_inactive: true })
      .then(next => setOverdue(Array.isArray(next) ? next : []))
      .catch(() => undefined);
  };

  const columns = useMemo<Array<DataTableColumn<Row>>>(
    () => [
      {
        key: 'vendor',
        header: 'Vendor',
        sortable: true,
        accessor: row => row.name,
        render: row => (
          <div>
            <div className="font-medium text-white">
              {row.name}
              {row.is_active === false && <InactiveTag />}
            </div>
            <div className="font-mono text-xs text-slate-400">{row.code}</div>
          </div>
        ),
        csv: row => `${row.code} ${row.name}${row.is_active === false ? ' (inactive)' : ''}`,
      },
      {
        key: 'approval_status',
        header: 'ASL Status',
        sortable: true,
        accessor: row => aslStatusLabel(row.approval_status),
        render: row => <AslStatusPill status={row.approval_status} />,
      },
      {
        key: 'approved_scopes',
        header: 'Scope of Supply',
        accessor: row => scopeLabels(row.approved_scopes),
        render: row => (
          <span className="block max-w-xs text-xs text-slate-300">{scopeLabels(row.approved_scopes) || '—'}</span>
        ),
      },
      {
        key: 'approval_bases',
        header: 'Basis',
        accessor: row => basisLabels(row.approval_bases),
        render: row => <span className="text-xs text-slate-300">{basisLabels(row.approval_bases) || '—'}</span>,
      },
      {
        key: 'approval_date',
        header: 'Approved',
        sortable: true,
        accessor: row => row.approval_date ?? '',
        render: row => <span className="text-slate-300">{dateText(row.approval_date)}</span>,
        csv: row => row.approval_date ?? '',
      },
      {
        key: 'reevaluation_due',
        header: 'Re-eval Due',
        sortable: true,
        accessor: row => row.reevaluation_due ?? '',
        render: reevalCell,
        csv: row => row.reevaluation_due ?? '',
      },
      {
        key: 'last_performance_evaluation',
        header: 'Last Perf Eval',
        sortable: true,
        accessor: row => row.last_performance_evaluation ?? '',
        render: row => <span className="text-slate-300">{dateText(row.last_performance_evaluation)}</span>,
        csv: row => row.last_performance_evaluation ?? '',
      },
      ...(canWrite
        ? [
            {
              key: 'actions',
              header: 'Actions',
              align: 'right' as const,
              render: (row: Row) => (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setEditing(row)}
                  aria-label={`Update approval for ${row.name}`}
                >
                  Update approval
                </Button>
              ),
            },
          ]
        : []),
    ],
    [canWrite]
  );

  const mobileCard = (row: Row) => (
    <MobileDataCard
      title={row.name}
      subtitle={row.is_active === false ? `${row.code} · Inactive` : row.code}
      badge={<AslStatusPill status={row.approval_status} />}
      fields={[
        { label: 'Scope', value: scopeLabels(row.approved_scopes) || '—', fullWidth: true },
        { label: 'Basis', value: basisLabels(row.approval_bases) || '—' },
        { label: 'Approved', value: dateText(row.approval_date) },
        { label: 'Re-eval due', value: reevalCell(row) },
        { label: 'Last perf eval', value: dateText(row.last_performance_evaluation) },
      ]}
      actions={
        canWrite ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setEditing(row)}
            aria-label={`Update approval for ${row.name}`}
          >
            Update approval
          </Button>
        ) : undefined
      }
    />
  );

  const chips: Array<{ id: AslFilter; label: string; count: number; alert?: boolean }> = [
    { id: 'all', label: 'All vendors', count: counts.all },
    { id: 'on', label: 'On the ASL', count: counts.on },
    { id: 'off', label: 'Not on the ASL', count: counts.off },
    { id: 'overdue', label: 'Re-evaluation overdue', count: counts.overdue, alert: counts.overdue > 0 },
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label="Approved Supplier List filter" className="flex flex-wrap gap-1">
          {chips.map(chip => (
            <button
              key={chip.id}
              type="button"
              aria-pressed={filter === chip.id}
              onClick={() => onFilterChange(chip.id)}
              className={`inline-flex items-center gap-1.5 rounded-sm border px-2.5 py-1 text-xs font-medium ${
                filter === chip.id
                  ? 'border-werco-navy-500 bg-werco-navy-600 text-white'
                  : chip.alert
                    ? 'border-red-500/50 bg-red-500/10 text-red-300 hover:bg-red-500/20'
                    : 'border-slate-600 bg-transparent text-slate-300 hover:bg-slate-800'
              }`}
            >
              {chip.label}
              <span className="font-mono">{loading ? '…' : chip.count}</span>
            </button>
          ))}
        </div>
        {canWrite && (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setImportOpen(true)}
            className="inline-flex items-center"
          >
            <ArrowUpTrayIcon className="mr-1 h-4 w-4" aria-hidden="true" />
            Import ASL
          </Button>
        )}
      </div>

      <DataTable<Row>
        columns={columns}
        data={rows}
        rowKey={row => row.id}
        defaultSort={filter === 'overdue' ? { key: 'reevaluation_due', dir: 'asc' } : { key: 'vendor', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: filter === 'overdue' ? 'asl-reevaluation-overdue' : 'approved-supplier-list' }}
        loading={loading}
        error={loadError}
        onRetry={load}
        mobileCards={mobileCard}
        empty={{
          icon: ShieldCheckIcon,
          title:
            filter === 'overdue'
              ? 'No re-evaluations overdue'
              : filter === 'on'
                ? 'No vendors on the ASL yet'
                : 'No vendors found',
          description: search
            ? 'No vendors match your search.'
            : filter === 'on' && canWrite
              ? 'Import the QL 7.4 sheet or update a vendor’s approval to add it to the list.'
              : undefined,
        }}
      />

      <UpdateApprovalDialog vendor={editing} onClose={() => setEditing(null)} onSaved={handleSaved} />
      {importOpen && (
        <AslImportWizard
          onClose={() => setImportOpen(false)}
          onCommitted={() => {
            void load();
          }}
        />
      )}
    </div>
  );
}

export default ApprovedSupplierListPanel;
