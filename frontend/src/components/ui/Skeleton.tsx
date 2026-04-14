/**
 * Skeleton Loading Components
 * 
 * Provides consistent loading states across the application.
 * Uses CSS animations for smooth pulsing effect.
 */

import React from 'react';

interface SkeletonProps {
  className?: string;
  width?: string | number;
  height?: string | number;
}

// Base skeleton with pulse animation
export const Skeleton: React.FC<SkeletonProps> = ({ 
  className = '', 
  width, 
  height 
}) => {
  const style: React.CSSProperties = {};
  if (width) style.width = typeof width === 'number' ? `${width}px` : width;
  if (height) style.height = typeof height === 'number' ? `${height}px` : height;

  return (
    <div 
      className={`animate-pulse bg-slate-700 rounded ${className}`}
      style={style}
      data-testid="skeleton"
    />
  );
};

// Text line skeleton (used internally by SkeletonCard)
const SkeletonText: React.FC<{ lines?: number; className?: string }> = ({
  lines = 1, 
  className = '' 
}) => (
  <div className={`space-y-2 ${className}`}>
    {Array.from({ length: lines }).map((_, i) => (
      <Skeleton 
        key={i} 
        className={`h-4 ${i === lines - 1 && lines > 1 ? 'w-3/4' : 'w-full'}`} 
      />
    ))}
  </div>
);

// Badge/tag skeleton (used internally by SkeletonCard)
const SkeletonBadge: React.FC = () => (
  <Skeleton className="h-6 w-16 rounded-full" />
);

// Card skeleton
export const SkeletonCard: React.FC<{ className?: string }> = ({ className = '' }) => (
  <div className={`bg-[#151b28] rounded-lg shadow p-6 ${className}`} data-testid="skeleton-card">
    <div className="animate-pulse space-y-4">
      <Skeleton className="h-6 w-1/3" />
      <SkeletonText lines={3} />
      <div className="flex gap-2 pt-2">
        <SkeletonBadge />
        <SkeletonBadge />
      </div>
    </div>
  </div>
);

// Table row skeleton (used internally by SkeletonTable)
const SkeletonTableRow: React.FC<{ columns: number }> = ({ columns }) => (
  <tr className="animate-pulse">
    {Array.from({ length: columns }).map((_, i) => (
      <td key={i} className="px-4 py-4">
        <Skeleton className="h-4 w-full" />
      </td>
    ))}
  </tr>
);

// Full table skeleton
export const SkeletonTable: React.FC<{ 
  rows?: number; 
  columns?: number;
  showHeader?: boolean;
}> = ({ rows = 5, columns = 6, showHeader = true }) => (
  <div className="overflow-hidden">
    <table className="min-w-full divide-y divide-slate-700">
      {showHeader && (
        <thead className="bg-slate-800" data-testid="skeleton-table-head">
          <tr>
            {Array.from({ length: columns }).map((_, i) => (
              <th key={i} className="px-4 py-3">
                <Skeleton className="h-4 w-20" />
              </th>
            ))}
          </tr>
        </thead>
      )}
      <tbody className="bg-[#151b28] divide-y divide-slate-700" data-testid="skeleton-table-body">
        {Array.from({ length: rows }).map((_, i) => (
          <SkeletonTableRow key={i} columns={columns} />
        ))}
      </tbody>
    </table>
  </div>
);

// Stats card skeleton (for dashboard)
export const SkeletonStatCard: React.FC = () => (
  <div className="bg-[#151b28] rounded-lg shadow p-6 animate-pulse" data-testid="skeleton-stat-card">
    <div className="flex items-center justify-between">
      <div className="space-y-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-8 w-16" />
      </div>
      <Skeleton className="h-12 w-12 rounded-lg" />
    </div>
  </div>
);

// Dashboard skeleton
export const SkeletonDashboard: React.FC = () => (
  <div className="space-y-6" data-testid="skeleton-dashboard">
    {/* Stats row */}
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <SkeletonStatCard key={i} />
      ))}
    </div>
    
    {/* Charts row */}
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <SkeletonCard className="h-80" />
      <SkeletonCard className="h-80" />
    </div>
    
    {/* Table */}
    <div className="bg-[#151b28] rounded-lg shadow">
      <div className="p-4 border-b">
        <Skeleton className="h-6 w-48" />
      </div>
      <SkeletonTable rows={5} columns={6} />
    </div>
  </div>
);

// Form skeleton
export const SkeletonForm: React.FC<{ fields?: number }> = ({ fields = 4 }) => (
  <div className="space-y-6 animate-pulse" data-testid="skeleton-form">
    {Array.from({ length: fields }).map((_, i) => (
      <div key={i} className="space-y-2" data-testid="skeleton-form-field">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-10 w-full rounded-md" />
      </div>
    ))}
    <div className="flex gap-3 pt-4">
      <Skeleton className="h-10 w-32 rounded-md" />
      <Skeleton className="h-10 w-24 rounded-md" />
    </div>
  </div>
);

// Detail page skeleton
export const SkeletonDetail: React.FC = () => (
  <div className="space-y-6" data-testid="skeleton-detail">
    {/* Header */}
    <div className="flex items-center justify-between">
      <div className="space-y-2">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-4 w-32" />
      </div>
      <div className="flex gap-2">
        <Skeleton className="h-10 w-24 rounded-md" />
        <Skeleton className="h-10 w-24 rounded-md" />
      </div>
    </div>
    
    {/* Content cards */}
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <SkeletonCard />
      </div>
      <div>
        <SkeletonCard />
      </div>
    </div>
    
    {/* Additional section */}
    <SkeletonCard />
  </div>
);

// List item skeleton
export const SkeletonListItem: React.FC = () => (
  <div className="flex items-center gap-4 p-4 animate-pulse" data-testid="skeleton-list-item">
    <Skeleton className="h-10 w-10 rounded-full" />
    <div className="flex-1 space-y-2">
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-3 w-1/2" />
    </div>
    <SkeletonBadge />
  </div>
);

// List skeleton
export const SkeletonList: React.FC<{ items?: number }> = ({ items = 5 }) => (
  <div className="divide-y divide-slate-700">
    {Array.from({ length: items }).map((_, i) => (
      <SkeletonListItem key={i} />
    ))}
  </div>
);

// Loading spinner (for inline/button loading)
export const Spinner: React.FC<{ size?: 'sm' | 'md' | 'lg'; className?: string }> = ({ 
  size = 'md',
  className = ''
}) => {
  const sizes = {
    sm: 'h-4 w-4 border-2',
    md: 'h-6 w-6 border-2',
    lg: 'h-8 w-8 border-3'
  };
  return (
    <div 
      className={`animate-spin rounded-full border-slate-600 border-t-werco-navy-400 ${sizes[size]} ${className}`}
      role="status"
      aria-label="Loading"
    />
  );
};

// Full page loading overlay
export const LoadingOverlay: React.FC<{ message?: string }> = ({ message = 'Loading...' }) => (
  <div className="fixed inset-0 bg-[#0d1117]/80 backdrop-blur-sm flex items-center justify-center z-50" data-testid="loading-overlay">
    <div className="text-center space-y-4">
      <Spinner size="lg" className="mx-auto" />
      <p className="text-slate-400 font-medium">{message}</p>
    </div>
  </div>
);

// Inline loading state
export const LoadingInline: React.FC<{ message?: string }> = ({ message = 'Loading...' }) => (
  <div className="flex items-center justify-center py-8" data-testid="loading-inline">
    <Spinner size="md" className="mr-3" />
    <span className="text-slate-400">{message}</span>
  </div>
);

export default Skeleton;
