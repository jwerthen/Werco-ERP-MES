/**
 * Quality's worklist of live work orders created without their process sheets
 * (GET /work-orders/process-sheet-gaps, ADMIN / MANAGER / QUALITY).
 *
 * A work order is listed when its part's released routing (or a BOM component's)
 * carries process sheets but none of its operations holds a snapshotted step, so the
 * kiosk shows none of the required checks. The routing is read as it is today, so a
 * job created before a sheet was attached is listed too. Pure read: this panel writes
 * nothing and offers no fix-up action; the job number links to the work order.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { DocumentMagnifyingGlassIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { DataTable, MobileDataCard, StatusBadge } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatCentralDate, getDateSortValue } from '../../utils/centralTime';
import { formatOperationLabel } from '../../utils/operationLabel';
import { usePermissions } from '../../hooks/usePermissions';
import { canAccessPath } from '../../utils/routeAccess';
import type { WorkOrderProcessSheetGap, WorkOrderProcessSheetGapStep } from '../../types/workOrderProcessSheet';

export const PROCESS_SHEET_GAP_LIMIT = 200;

const stepText = (step: WorkOrderProcessSheetGapStep) => {
  const owner = step.component_part_number ? `${step.component_part_number}: ` : '';
  const sheet = step.sheet_number ?? (step.process_sheet_id != null ? `#${step.process_sheet_id}` : '?');
  return `${owner}${formatOperationLabel(step.operation_number, step.operation_sequence)} ${step.name} (sheet ${sheet})`;
};

export default function ProcessSheetGapsPanel() {
  const { can } = usePermissions();
  const canOpenWorkOrders = canAccessPath('/work-orders/1', can);
  const [rows, setRows] = useState<WorkOrderProcessSheetGap[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const response = await api.getProcessSheetGaps(PROCESS_SHEET_GAP_LIMIT);
      setRows(response.items);
      setTotal(response.total);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const workOrderCell = useCallback(
    (row: WorkOrderProcessSheetGap) =>
      canOpenWorkOrders ? (
        <Link to={`/work-orders/${row.work_order_id}`} className="font-mono text-werco-primary hover:underline">
          {row.work_order_number}
        </Link>
      ) : (
        <span className="font-mono">{row.work_order_number}</span>
      ),
    [canOpenWorkOrders]
  );

  const columns = useMemo<DataTableColumn<WorkOrderProcessSheetGap>[]>(
    () => [
      {
        key: 'work_order_number',
        header: 'Work Order',
        sortable: true,
        accessor: row => row.work_order_number,
        render: workOrderCell,
      },
      {
        key: 'status',
        header: 'Status',
        render: row => <StatusBadge status={row.status} />,
        accessor: row => row.status,
      },
      {
        key: 'part_number',
        header: 'Part',
        sortable: true,
        accessor: row => row.part_number,
        render: row => (
          <div className="min-w-0">
            <div className="font-mono">{row.part_number}</div>
            {row.part_name && <div className="text-xs text-slate-400 truncate">{row.part_name}</div>}
          </div>
        ),
      },
      { key: 'customer_name', header: 'Customer', accessor: row => row.customer_name ?? '' },
      {
        key: 'due_date',
        header: 'Due',
        sortable: true,
        accessor: row => (row.due_date ? getDateSortValue(row.due_date) : null),
        render: row => (row.due_date ? formatCentralDate(row.due_date) : '—'),
        csv: row => row.due_date ?? '',
      },
      {
        key: 'steps',
        header: 'Sheet steps not on the job',
        accessor: row => row.expected_process_sheet_steps.length,
        render: row => (
          <ul className="space-y-0.5 text-xs text-slate-300">
            {row.expected_process_sheet_steps.map(step => (
              <li key={step.routing_operation_id}>{stepText(step)}</li>
            ))}
          </ul>
        ),
        csv: row => row.expected_process_sheet_steps.map(stepText).join('; '),
      },
      {
        key: 'created_at',
        header: 'Created',
        sortable: true,
        accessor: row => (row.created_at ? getDateSortValue(row.created_at) : null),
        render: row => (row.created_at ? formatCentralDate(row.created_at) : '—'),
        csv: row => (row.created_at ? formatCentralDate(row.created_at) : ''),
      },
    ],
    [workOrderCell]
  );

  return (
    <>
      <div className="mb-4">
        <h2 className="text-lg font-semibold">Work Orders Missing Process Sheets</h2>
        <p className="text-sm text-slate-400 mt-0.5">
          Live work orders whose released routing carries process sheets, but whose operations hold none of the sheet
          steps, so the kiosk shows none of the required checks. Routings are read as they are today.
        </p>
        {!loading && !error && total > rows.length && (
          <p className="text-sm text-amber-300 mt-1">
            Showing the first {rows.length} of {total} work orders, by due date.
          </p>
        )}
      </div>
      <DataTable
        columns={columns}
        data={rows}
        rowKey={row => row.work_order_id}
        loading={loading}
        error={error ? 'Could not load work orders missing process sheets.' : undefined}
        onRetry={() => void load()}
        pageSize={25}
        csvExport={{ filename: 'work-orders-missing-process-sheets' }}
        empty={{
          icon: DocumentMagnifyingGlassIcon,
          title: 'No work orders are missing process sheets',
          description: 'Every live work order whose routing carries process sheets holds its sheet steps.',
        }}
        mobileCards={row => (
          <MobileDataCard
            title={row.work_order_number}
            subtitle={row.part_name ? `${row.part_number} — ${row.part_name}` : row.part_number}
            badge={<StatusBadge status={row.status} />}
            fields={[
              { label: 'Customer', value: row.customer_name || '—' },
              { label: 'Due', value: row.due_date ? formatCentralDate(row.due_date) : '—' },
              {
                label: 'Sheet steps',
                value: row.expected_process_sheet_steps.map(stepText).join('; '),
              },
              { label: 'Open', value: workOrderCell(row) },
            ]}
          />
        )}
      />
    </>
  );
}
