// UI Components barrel export
export * from './Skeleton';
export * from './LoadingButton';
export * from './SelectField';
export * from './Modal';

// Previously deep-import-only primitives — surfaced here so callers can import
// them from '@/components/ui' alongside the rest of the kit.
export { StatusBadge } from './StatusBadge';
export { Tabs } from './Tabs';
export type { Tab } from './Tabs';
export { ConfirmDialog } from './ConfirmDialog';
export { Breadcrumbs } from './Breadcrumbs';
export type { Crumb } from './Breadcrumbs';

// Mobile-responsive components
export { MobileDataCard, MobileDataList, ResponsiveDataView } from './MobileDataCard';
export { default as BottomNav } from './BottomNav';
