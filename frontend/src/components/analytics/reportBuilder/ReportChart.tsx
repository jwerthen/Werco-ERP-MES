import React, { useId, useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type {
  CustomReportResult,
  ReportDataSourceCatalog,
  ReportDefinition,
  ReportResultColumn,
} from '../../../types/reportBuilder';
import { findField } from './reportDefinition';
import { formatReportValue } from './formatReportValue';

/** Beyond this many groups a chart is unreadable; the table still has every row. */
export const MAX_CHART_POINTS = 200;

const SERIES_COLORS = ['#1B4D9C', '#C8352B', '#3fb950', '#d29922', '#39c5cf', '#94a3b8'];

export interface ChartSpec {
  kind: 'bar' | 'line';
  category: ReportResultColumn;
  series: ReportResultColumn[];
}

/**
 * What to chart, or null: only a GROUPED result with at least one number column
 * besides the first group-by field. The first group-by field is the category axis;
 * a date / datetime one makes it a line chart over time, anything else a bar chart.
 */
export function chartSpec(
  result: CustomReportResult,
  definition: ReportDefinition,
  catalog: ReportDataSourceCatalog
): ChartSpec | null {
  const firstGroup = definition.group_by[0]?.field;
  if (!firstGroup) return null;
  const groupColumn = definition.columns.find(column => column.field === firstGroup && !column.aggregate);
  const categoryKey = groupColumn ? groupColumn.alias?.trim() || groupColumn.field : firstGroup;
  const category = result.columns.find(column => column.key === categoryKey);
  if (!category) return null;
  const series = result.columns.filter(column => column.type === 'number' && column.key !== category.key);
  if (series.length === 0) return null;
  const type = findField(catalog, definition.data_source, firstGroup)?.type ?? category.type;
  return { kind: type === 'date' || type === 'datetime' ? 'line' : 'bar', category, series };
}

interface ReportChartProps {
  spec: ChartSpec;
  result: CustomReportResult;
}

const AXIS_TICK = { fontSize: 12, fill: '#94a3b8' };
const TOOLTIP_STYLE = {
  backgroundColor: '#1a1f2e',
  border: '1px solid #334155',
  borderRadius: '2px',
  color: '#e2e8f0',
};

/**
 * The grouped result as a chart. Decorative to assistive tech (the caption says so):
 * the table under it is the data of record and carries every value.
 */
export function ReportChart({ spec, result }: ReportChartProps) {
  const captionId = useId();
  const total = result.rows.length;
  const data = useMemo(() => {
    let rows = result.rows.slice(0, MAX_CHART_POINTS);
    if (spec.kind === 'line') {
      // A line over time reads left to right in time, whatever order the table is sorted in.
      rows = rows
        .slice()
        .sort((a, b) => String(a[spec.category.key] ?? '').localeCompare(String(b[spec.category.key] ?? '')));
    }
    return rows.map(row => {
      const point: Record<string, string | number | null> = {
        __category: formatReportValue(row[spec.category.key], spec.category.type),
      };
      spec.series.forEach(column => {
        const value = row[column.key];
        point[column.key] = typeof value === 'number' ? value : null;
      });
      return point;
    });
  }, [result.rows, spec]);

  const seriesNames = spec.series.map(column => column.label).join(', ');
  const kindLabel = spec.kind === 'line' ? 'Line chart' : 'Bar chart';

  return (
    <figure aria-labelledby={captionId} className="m-0">
      <figcaption id={captionId} className="mb-2 text-xs text-fd-mute">
        {`${kindLabel} of ${seriesNames} by ${spec.category.label}.`}
        {total > MAX_CHART_POINTS ? ` Shows the first ${MAX_CHART_POINTS} of ${total.toLocaleString()} groups.` : ''}
        {' The table below is the data of record.'}
      </figcaption>
      <div className="h-72" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          {spec.kind === 'line' ? (
            <LineChart data={data} accessibilityLayer={false}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="__category" tick={AXIS_TICK} />
              <YAxis tick={AXIS_TICK} />
              <Tooltip contentStyle={TOOLTIP_STYLE} />
              <Legend />
              {spec.series.map((column, index) => (
                <Line
                  key={column.key}
                  type="monotone"
                  dataKey={column.key}
                  name={column.label}
                  stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          ) : (
            <BarChart data={data} accessibilityLayer={false}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="__category" tick={AXIS_TICK} />
              <YAxis tick={AXIS_TICK} />
              <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(148, 163, 184, 0.08)' }} />
              <Legend />
              {spec.series.map((column, index) => (
                <Bar
                  key={column.key}
                  dataKey={column.key}
                  name={column.label}
                  fill={SERIES_COLORS[index % SERIES_COLORS.length]}
                  isAnimationActive={false}
                />
              ))}
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export default ReportChart;
