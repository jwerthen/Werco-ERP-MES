// UI Components barrel export
export * from './Skeleton';
export * from './LoadingButton';
export * from './SelectField';
export * from './FormField';
export * from './Modal';
export { EmptyState } from './EmptyState';
export { ErrorState } from './ErrorState';
export { useToast, ToastProvider } from './Toast';

// Previously deep-import-only primitives — surfaced here so callers can import
// them from '@/components/ui' alongside the rest of the kit.
export { StatusBadge } from './StatusBadge';
export { Button } from './Button';
export type { ButtonProps, ButtonVariant, ButtonSize } from './Button';
export {
  statusVariant,
  statusColor,
  statusColorMap,
  variantClass as statusVariantClass,
  UNKNOWN_STATUS_CLASS,
} from '../../utils/statusColors';
export type { StatusVariant } from '../../utils/statusColors';
export { Tabs } from './Tabs';
export type { Tab } from './Tabs';
export { ConfirmDialog } from './ConfirmDialog';
export { Breadcrumbs } from './Breadcrumbs';
export type { Crumb } from './Breadcrumbs';

// Generic data table (sort / paginate / select / CSV / responsive)
export { DataTable, buildCsv } from './DataTable';
export type {
  DataTableProps,
  DataTableColumn,
  DataTableEmpty,
  DataTableSelection,
  DataTableServerPagination,
  SortDir,
  ColumnAlign,
} from './DataTable';

// Mobile-responsive components
export { MobileDataCard, MobileDataList, ResponsiveDataView } from './MobileDataCard';
export { default as BottomNav } from './BottomNav';
