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
      className={`animate-pulse bg-gray-200 rounded ${className}`}
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
  <div className={`bg-white rounded-lg shadow p-6 ${className}`} data-testid="skeleton-card">
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
    <table className="min-w-full divide-y divide-gray-200">
      {showHeader && (
        <thead className="bg-gray-50" data-testid="skeleton-table-head">
          <tr>
            {Array.from({ length: columns }).map((_, i) => (
              <th key={i} className="px-4 py-3">
                <Skeleton className="h-4 w-20" />
              </th>
            ))}
          </tr>
        </thead>
      )}
      <tbody className="bg-white divide-y divide-gray-200" data-testid="skeleton-table-body">
        {Array.from({ length: rows }).map((_, i) => (
          <SkeletonTableRow key={i} columns={columns} />
        ))}
      </tbody>
    </table>
  </div>
);

// Stats card skeleton (used internally by SkeletonDashboard)
const SkeletonStatCard: React.FC = () => (
  <div className="bg-white rounded-lg shadow p-6 animate-pulse" data-testid="skeleton-stat-card">
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
    <div className="bg-white rounded-lg shadow">
      <div className="p-4 border-b">
        <Skeleton className="h-6 w-48" />
      </div>
      <SkeletonTable rows={5} columns={6} />
    </div>
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
      className={`animate-spin rounded-full border-gray-300 border-t-werco-navy-600 ${sizes[size]} ${className}`}
      role="status"
      aria-label="Loading"
    />
  );
};

// Full page loading overlay
export const LoadingOverlay: React.FC<{ message?: string }> = ({ message = 'Loading...' }) => (
  <div className="fixed inset-0 bg-white/80 backdrop-blur-sm flex items-center justify-center z-50" data-testid="loading-overlay">
    <div className="text-center space-y-4">
      <Spinner size="lg" className="mx-auto" />
      <p className="text-gray-600 font-medium">{message}</p>
    </div>
  </div>
);

export default Skeleton;
