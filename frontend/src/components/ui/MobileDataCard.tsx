import React from 'react';
import { ChevronRightIcon } from '@heroicons/react/24/outline';

interface DataField {
  label: string;
  value: React.ReactNode;
  className?: string;
  fullWidth?: boolean;
}

interface MobileDataCardProps {
  title: string;
  subtitle?: string;
  badge?: React.ReactNode;
  fields: DataField[];
  actions?: React.ReactNode;
  onClick?: () => void;
  className?: string;
  highlight?: boolean;
}

export function MobileDataCard({
  title,
  subtitle,
  badge,
  fields,
  actions,
  onClick,
  className = '',
  highlight = false,
}: MobileDataCardProps) {
  const isClickable = !!onClick;

  return (
    <div
      className={`
        bg-white rounded-xl border border-slate-200 overflow-hidden
        ${highlight ? 'ring-2 ring-cyan-500 ring-offset-2' : ''}
        ${isClickable ? 'cursor-pointer active:bg-slate-50 touch:active:scale-[0.99]' : ''}
        transition-all duration-200
        ${className}
      `}
      onClick={onClick}
      role={isClickable ? 'button' : undefined}
      tabIndex={isClickable ? 0 : undefined}
    >
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-100 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold text-slate-900 truncate">{title}</h3>
            {badge}
          </div>
          {subtitle && (
            <p className="text-sm text-slate-500 truncate mt-0.5">{subtitle}</p>
          )}
        </div>
        {isClickable && (
          <ChevronRightIcon className="h-5 w-5 text-slate-400 flex-shrink-0" />
        )}
      </div>

      {/* Fields Grid */}
      <div className="px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2">
        {fields.map((field, index) => (
          <div
            key={index}
            className={`${field.fullWidth ? 'col-span-2' : ''} ${field.className || ''}`}
          >
            <dt className="text-xs text-slate-500 uppercase tracking-wide">
              {field.label}
            </dt>
            <dd className="text-sm font-medium text-slate-900 mt-0.5">
              {field.value}
            </dd>
          </div>
        ))}
      </div>

      {/* Actions */}
      {actions && (
        <div className="px-4 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-2">
          {actions}
        </div>
      )}
    </div>
  );
}

interface MobileDataListProps {
  children: React.ReactNode;
  className?: string;
}

export function MobileDataList({ children, className = '' }: MobileDataListProps) {
  return (
    <div className={`space-y-3 ${className}`}>
      {children}
    </div>
  );
}

interface ResponsiveDataViewProps<T> {
  data: T[];
  renderCard: (item: T, index: number) => React.ReactNode;
  renderTable: () => React.ReactNode;
  loading?: boolean;
  loadingCard?: React.ReactNode;
  loadingTable?: React.ReactNode;
  emptyMessage?: string;
  breakpoint?: 'sm' | 'md' | 'lg';
}

export function ResponsiveDataView<T>({
  data,
  renderCard,
  renderTable,
  loading = false,
  loadingCard,
  loadingTable,
  emptyMessage = 'No data available',
  breakpoint = 'md',
}: ResponsiveDataViewProps<T>) {
  const breakpointClass = {
    sm: 'sm:hidden',
    md: 'md:hidden',
    lg: 'lg:hidden',
  }[breakpoint];

  const tableBreakpointClass = {
    sm: 'hidden sm:block',
    md: 'hidden md:block',
    lg: 'hidden lg:block',
  }[breakpoint];

  if (loading) {
    return (
      <>
        {/* Mobile Loading */}
        <div className={breakpointClass}>
          {loadingCard || (
            <MobileDataList>
              {[...Array(3)].map((_, i) => (
                <div key={i} className="bg-white rounded-xl border border-slate-200 p-4 animate-pulse">
                  <div className="h-5 bg-slate-200 rounded w-1/2 mb-3" />
                  <div className="grid grid-cols-2 gap-3">
                    <div className="h-4 bg-slate-100 rounded" />
                    <div className="h-4 bg-slate-100 rounded" />
                    <div className="h-4 bg-slate-100 rounded" />
                    <div className="h-4 bg-slate-100 rounded" />
                  </div>
                </div>
              ))}
            </MobileDataList>
          )}
        </div>

        {/* Desktop Loading */}
        <div className={tableBreakpointClass}>
          {loadingTable || (
            <div className="bg-white rounded-xl border border-slate-200 p-4 animate-pulse">
              <div className="h-8 bg-slate-200 rounded mb-4" />
              {[...Array(5)].map((_, i) => (
                <div key={i} className="h-12 bg-slate-100 rounded mb-2" />
              ))}
            </div>
          )}
        </div>
      </>
    );
  }

  if (data.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 p-8 text-center">
        <p className="text-slate-500">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <>
      {/* Mobile Card View */}
      <div className={breakpointClass}>
        <MobileDataList>
          {data.map((item, index) => renderCard(item, index))}
        </MobileDataList>
      </div>

      {/* Desktop Table View */}
      <div className={tableBreakpointClass}>{renderTable()}</div>
    </>
  );
}

export default MobileDataCard;
