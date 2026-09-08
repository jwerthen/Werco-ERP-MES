export type MetricName = 'LCP' | 'INP' | 'CLS';
export type MetricDevice = 'mobile' | 'tablet' | 'desktop';

export interface RuntimeMetricSample {
  metric_id: string;
  name: MetricName;
  route: string;
  device: MetricDevice;
  navigation: 'document' | 'soft';
  release: string;
  value: number;
  sequence: number;
}

export interface RuntimeMetricSummaryRow {
  route: string;
  device: MetricDevice;
  name: MetricName;
  navigation: 'document' | 'soft';
  release: string;
  samples: number;
  p75: number;
  good_percent: number;
}

export interface RuntimeMetricSummary {
  enabled: boolean;
  retention_days: number;
  days: number;
  page: number;
  has_more: boolean;
  rows: RuntimeMetricSummaryRow[];
}
