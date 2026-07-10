/**
 * ShipOtdReport — the Reports "Ship OTD" tab (Lean Phase 1 / issue #88).
 *
 * Ship-based on-time delivery, measured against the promise
 * (must_ship_by || due_date):
 *   - headline OTD (shipped) and OTIF percentages,
 *   - a per-customer rollup table,
 *   - the per-work-order detail rows (promise vs first/last/full ship dates),
 *   - a "Promise hygiene" list — shipped/open WOs with NEITHER promise field,
 *     which are unmeasurable and silently poison OTD if left unfixed.
 *
 * Null percentages mean an empty denominator — rendered "—", never a fake 100.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowPathIcon,
  CalendarDaysIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  TruckIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../cockpit';
import { DataTable, DataTableColumn, ErrorState, StatusBadge, statusVariantClass } from '../ui';
import { formatCentralDate } from '../../utils/centralTime';
import type {
  PromiseHygieneRow,
  ShipOtdCustomerRollup,
  ShipOtdReportResponse,
  ShipOtdRow,
} from '../../types/leanAnalytics';

function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toFixed(1)}%`;
}

const shortDate = (value: string | null) =>
  value ? formatCentralDate(value, { month: 'short', day: 'numeric' }) : '—';

function OnTimeBadge({ row }: { row: ShipOtdRow }) {
  if (row.on_time === true) {
    return (
      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusVariantClass.green}`}>
        On time
      </span>
    );
  }
  if (row.on_time === false) {
    return (
      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium tabular-nums ${statusVariantClass.red}`}>
        Late{row.days_late !== null ? ` +${row.days_late}d` : ''}
      </span>
    );
  }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${statusVariantClass.slate}`}>
      Open
    </span>
  );
}

interface ShipOtdReportProps {
  /** Reports-page period in days (7 / 30 / 90). */
  periodDays: number;
}

export default function ShipOtdReport({ periodDays }: ShipOtdReportProps) {
  const navigate = useNavigate();
  const [report, setReport] = useState<ShipOtdReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const period = periodDays === 7 ? '7d' : periodDays === 90 ? '90d' : '30d';

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const data = await api.getShipOtdReport({ period });
      setReport(data);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [period]);

  useEffect(() => {
    void load();
  }, [load]);

  const customerColumns = useMemo<Array<DataTableColumn<ShipOtdCustomerRollup>>>(
    () => [
      {
        key: 'customer_name',
        header: 'Customer',
        sortable: true,
        className: 'font-medium max-w-[220px] truncate',
        accessor: (row) => row.customer_name,
      },
      {
        key: 'work_orders',
        header: 'WOs',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.work_orders,
      },
      {
        key: 'on_time',
        header: 'On Time',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums text-fd-green',
        accessor: (row) => row.on_time,
      },
      {
        key: 'late',
        header: 'Late',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        accessor: (row) => row.late,
        render: (row) => (
          <span className={`tabular-nums ${row.late > 0 ? 'text-fd-red font-semibold' : 'text-slate-400'}`}>
            {row.late}
          </span>
        ),
      },
      {
        key: 'otd_pct',
        header: 'OTD',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        accessor: (row) => row.otd_pct ?? -1,
        csv: (row) => (row.otd_pct === null ? '' : row.otd_pct.toFixed(1)),
        render: (row) => (
          <span
            className={`font-semibold tabular-nums ${
              row.otd_pct === null
                ? 'text-slate-500'
                : row.otd_pct >= 95
                  ? 'text-fd-green'
                  : row.otd_pct >= 85
                    ? 'text-fd-amber'
                    : 'text-fd-red'
            }`}
          >
            {fmtPct(row.otd_pct)}
          </span>
        ),
      },
      {
        key: 'avg_days_late',
        header: 'Avg Days Late',
        sortable: true,
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.avg_days_late ?? -1,
        csv: (row) => (row.avg_days_late === null ? '' : row.avg_days_late.toFixed(1)),
        render: (row) => (row.avg_days_late === null ? '—' : row.avg_days_late.toFixed(1)),
      },
    ],
    []
  );

  const rowColumns = useMemo<Array<DataTableColumn<ShipOtdRow>>>(
    () => [
      {
        key: 'work_order_number',
        header: 'Work Order',
        sortable: true,
        className: 'font-medium',
        accessor: (row) => row.work_order_number,
      },
      {
        key: 'customer_name',
        header: 'Customer',
        sortable: true,
        className: 'text-sm text-slate-400 max-w-[160px] truncate',
        accessor: (row) => row.customer_name ?? '',
        render: (row) => row.customer_name || '-',
      },
      {
        key: 'part_number',
        header: 'Part',
        sortable: true,
        accessor: (row) => row.part_number ?? '',
        render: (row) => row.part_number || '-',
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (row) => row.status,
        csv: (row) => row.status.replace(/_/g, ' '),
        render: (row) => <StatusBadge status={row.status} />,
      },
      {
        key: 'qty',
        header: 'Shipped / Ordered',
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.quantity_shipped,
        csv: (row) => `${row.quantity_shipped}/${row.quantity_ordered}`,
        render: (row) => (
          <span className={row.fully_shipped ? 'text-fd-green' : undefined}>
            {row.quantity_shipped.toLocaleString()} / {row.quantity_ordered.toLocaleString()}
          </span>
        ),
      },
      {
        key: 'promise_date',
        header: 'Promise',
        sortable: true,
        accessor: (row) => row.promise_date ?? '',
        csv: (row) =>
          row.promise_date ? `${formatCentralDate(row.promise_date)} (${row.promise_source ?? ''})` : '',
        render: (row) =>
          row.promise_date ? (
            <span className="text-sm tabular-nums">
              {shortDate(row.promise_date)}
              <span className="ml-1 text-[10px] uppercase tracking-wide text-slate-500">
                {row.promise_source === 'must_ship_by' ? 'ship-by' : 'due'}
              </span>
            </span>
          ) : (
            <span className="text-slate-500">—</span>
          ),
      },
      {
        key: 'first_ship_date',
        header: 'First Ship',
        sortable: true,
        className: 'text-sm tabular-nums',
        accessor: (row) => row.first_ship_date ?? '',
        csv: (row) => (row.first_ship_date ? formatCentralDate(row.first_ship_date) : ''),
        render: (row) => shortDate(row.first_ship_date),
      },
      {
        key: 'full_ship_date',
        header: 'Fully Shipped',
        sortable: true,
        className: 'text-sm tabular-nums',
        accessor: (row) => row.full_ship_date ?? '',
        csv: (row) => (row.full_ship_date ? formatCentralDate(row.full_ship_date) : ''),
        render: (row) => shortDate(row.full_ship_date),
      },
      {
        key: 'on_time',
        header: 'On Time',
        sortable: true,
        accessor: (row) => (row.on_time === null ? 'open' : row.on_time ? 'on time' : 'late'),
        render: (row) => <OnTimeBadge row={row} />,
      },
    ],
    []
  );

  const hygieneColumns = useMemo<Array<DataTableColumn<PromiseHygieneRow>>>(
    () => [
      {
        key: 'work_order_number',
        header: 'Work Order',
        sortable: true,
        className: 'font-medium',
        accessor: (row) => row.work_order_number,
      },
      {
        key: 'customer_name',
        header: 'Customer',
        sortable: true,
        className: 'text-sm text-slate-400 max-w-[180px] truncate',
        accessor: (row) => row.customer_name ?? '',
        render: (row) => row.customer_name || '-',
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (row) => row.status,
        csv: (row) => row.status.replace(/_/g, ' '),
        render: (row) => <StatusBadge status={row.status} />,
      },
      {
        key: 'qty',
        header: 'Shipped / Ordered',
        align: 'right',
        headerClassName: 'text-right',
        className: 'tabular-nums',
        accessor: (row) => row.quantity_shipped,
        csv: (row) => `${row.quantity_shipped}/${row.quantity_ordered}`,
        render: (row) => `${row.quantity_shipped.toLocaleString()} / ${row.quantity_ordered.toLocaleString()}`,
      },
      {
        key: 'last_ship_date',
        header: 'Last Ship',
        sortable: true,
        className: 'text-sm tabular-nums',
        accessor: (row) => row.last_ship_date ?? '',
        csv: (row) => (row.last_ship_date ? formatCentralDate(row.last_ship_date) : ''),
        render: (row) => shortDate(row.last_ship_date),
      },
    ],
    []
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48">
        <ArrowPathIcon className="h-8 w-8 animate-spin text-werco-primary" />
      </div>
    );
  }

  if (loadError || !report) {
    return <ErrorState message="Could not load the ship OTD report." onRetry={load} />;
  }

  const measuredRows = report.rows.length;
  const fullyShipped = report.rows.filter((row) => row.fully_shipped).length;
  const hygieneCount = report.promise_hygiene.length;

  return (
    <div className="space-y-4">
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
        <MiniStat
          icon={TruckIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="OTD (shipped)"
          value={fmtPct(report.otd_ship_pct)}
          subtitle="fully shipped on/before promise"
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="OTIF"
          value={fmtPct(report.otif_pct)}
          subtitle="in full by the promise date"
        />
        <MiniStat
          icon={CalendarDaysIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="WOs Measured"
          value={measuredRows}
          subtitle={`${formatCentralDate(report.period_start, { month: 'short', day: 'numeric' })} – ${formatCentralDate(report.period_end, { month: 'short', day: 'numeric' })}`}
        />
        <MiniStat
          icon={TruckIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Fully Shipped"
          value={fullyShipped}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={hygieneCount > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
          iconColor={hygieneCount > 0 ? 'text-fd-amber' : 'text-fd-green'}
          label="Missing Promise"
          value={hygieneCount}
          valueColor={hygieneCount > 0 ? 'text-fd-amber' : undefined}
          subtitle="no ship-by or due date"
        />
      </MiniStatStrip>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
        <CockpitPanel
          title="OTD by Customer"
          subtitle="Fulfillment-anchored, this period"
          className="xl:col-span-5"
          bodyClassName="lg:max-h-none"
        >
          <DataTable
            columns={customerColumns}
            data={report.by_customer}
            rowKey={(row) => row.customer_name}
            defaultSort={{ key: 'otd_pct', dir: 'asc' }}
            pageSize={10}
            csvExport={{ filename: 'ship-otd-by-customer' }}
            empty={{
              icon: TruckIcon,
              title: 'No measurable shipments',
              description: 'Customer OTD appears once promised work orders finish shipping in this period.',
            }}
          />
        </CockpitPanel>

        <CockpitPanel
          title="Work Orders"
          subtitle="Promise vs ship dates"
          className="xl:col-span-7"
          bodyClassName="lg:max-h-none"
          footer={measuredRows ? `${measuredRows} work orders` : undefined}
        >
          <DataTable
            columns={rowColumns}
            data={report.rows}
            rowKey={(row) => row.work_order_id}
            defaultSort={{ key: 'promise_date', dir: 'asc' }}
            pageSize={15}
            csvExport={{ filename: 'ship-otd-work-orders' }}
            onRowClick={(row) => navigate(`/work-orders/${row.work_order_id}`)}
            empty={{
              icon: TruckIcon,
              title: 'No shipments in this period',
              description: 'Work orders with a promise date and shipment activity appear here.',
            }}
          />
        </CockpitPanel>
      </div>

      {/* Promise hygiene — unmeasurable WOs. Rendered even when empty so the
          discipline ("every WO carries a promise") stays visible. */}
      <CockpitPanel
        title="Promise Hygiene"
        subtitle="Shipped or open work orders with neither a ship-by nor a due date — unmeasurable for OTD"
        bodyClassName="lg:max-h-none"
        headerExtra={
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold tabular-nums ${
              hygieneCount > 0 ? statusVariantClass.amber : statusVariantClass.green
            }`}
          >
            {hygieneCount}
          </span>
        }
      >
        <DataTable
          columns={hygieneColumns}
          data={report.promise_hygiene}
          rowKey={(row) => row.work_order_id}
          defaultSort={{ key: 'work_order_number', dir: 'asc' }}
          pageSize={10}
          csvExport={{ filename: 'promise-hygiene' }}
          onRowClick={(row) => navigate(`/work-orders/${row.work_order_id}`)}
          empty={{
            icon: CheckCircleIcon,
            title: 'All work orders carry a promise date',
            description: 'Every shipped/open WO in this period has a must-ship-by or due date.',
          }}
        />
      </CockpitPanel>
    </div>
  );
}
