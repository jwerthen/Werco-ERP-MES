/**
 * Skeleton Components Tests
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import {
  Skeleton,
  SkeletonText,
  SkeletonAvatar,
  SkeletonButton,
  SkeletonBadge,
  SkeletonCard,
  SkeletonTableRow,
  SkeletonTable,
  SkeletonStatCard,
  SkeletonDashboard,
  SkeletonForm,
  SkeletonDetail,
  SkeletonListItem,
  SkeletonList,
  Spinner,
  LoadingOverlay,
  LoadingInline,
} from './Skeleton';

describe('Skeleton Components', () => {
  describe('Skeleton', () => {
    it('renders with default props', () => {
      render(<Skeleton />);
      expect(screen.getByTestId('skeleton')).toHaveClass('animate-pulse', 'bg-gray-200', 'rounded');
    });

    it('applies custom className', () => {
      render(<Skeleton className="h-4 w-full" />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-4', 'w-full');
    });

    it('applies custom width and height', () => {
      render(<Skeleton width={100} height={50} />);
      expect(screen.getByTestId('skeleton')).toHaveStyle({ width: '100px', height: '50px' });
    });

    it('applies string width and height', () => {
      render(<Skeleton width="50%" height="2rem" />);
      expect(screen.getByTestId('skeleton')).toHaveStyle({ width: '50%', height: '2rem' });
    });
  });

  describe('SkeletonText', () => {
    it('renders single line by default', () => {
      render(<SkeletonText />);
      expect(screen.getAllByTestId('skeleton')).toHaveLength(1);
    });

    it('renders multiple lines', () => {
      render(<SkeletonText lines={3} />);
      expect(screen.getAllByTestId('skeleton')).toHaveLength(3);
    });

    it('last line is shorter when multiple lines', () => {
      render(<SkeletonText lines={3} />);
      const skeletons = screen.getAllByTestId('skeleton');
      expect(skeletons[2]).toHaveClass('w-3/4');
    });
  });

  describe('SkeletonAvatar', () => {
    it('renders medium size by default', () => {
      render(<SkeletonAvatar />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-10', 'w-10', 'rounded-full');
    });

    it('renders small size', () => {
      render(<SkeletonAvatar size="sm" />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-8', 'w-8');
    });

    it('renders large size', () => {
      render(<SkeletonAvatar size="lg" />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-12', 'w-12');
    });
  });

  describe('SkeletonButton', () => {
    it('renders with default width', () => {
      render(<SkeletonButton />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-9', 'w-24', 'rounded-md');
    });

    it('renders with custom width', () => {
      render(<SkeletonButton width="w-32" />);
      expect(screen.getByTestId('skeleton')).toHaveClass('w-32');
    });
  });

  describe('SkeletonBadge', () => {
    it('renders badge skeleton', () => {
      render(<SkeletonBadge />);
      expect(screen.getByTestId('skeleton')).toHaveClass('h-6', 'w-16', 'rounded-full');
    });
  });

  describe('SkeletonCard', () => {
    it('renders card with skeleton content', () => {
      render(<SkeletonCard />);
      const card = screen.getByTestId('skeleton-card');
      expect(card).toHaveClass('bg-white');
      expect(card).toHaveClass('rounded-lg');
    });

    it('applies custom className', () => {
      render(<SkeletonCard className="h-80" />);
      expect(screen.getByTestId('skeleton-card')).toHaveClass('h-80');
    });
  });

  describe('SkeletonTable', () => {
    it('renders default rows and columns', () => {
      render(<SkeletonTable />);
      const body = screen.getByTestId('skeleton-table-body');
      expect(within(body).getAllByRole('row')).toHaveLength(5);
    });

    it('renders custom rows and columns', () => {
      render(<SkeletonTable rows={3} columns={4} />);
      const body = screen.getByTestId('skeleton-table-body');
      const rows = within(body).getAllByRole('row');
      expect(rows).toHaveLength(3);
      expect(within(rows[0]).getAllByRole('cell')).toHaveLength(4);
    });

    it('hides header when showHeader is false', () => {
      render(<SkeletonTable showHeader={false} />);
      expect(screen.queryByTestId('skeleton-table-head')).not.toBeInTheDocument();
    });
  });

  describe('SkeletonTableRow', () => {
    it('renders correct number of columns', () => {
      render(
        <table><tbody><SkeletonTableRow columns={5} /></tbody></table>
      );
      expect(screen.getAllByRole('cell')).toHaveLength(5);
    });
  });

  describe('SkeletonStatCard', () => {
    it('renders stat card skeleton', () => {
      render(<SkeletonStatCard />);
      const card = screen.getByTestId('skeleton-stat-card');
      expect(card).toHaveClass('bg-white');
      expect(card).toHaveClass('animate-pulse');
    });
  });

  describe('SkeletonDashboard', () => {
    it('renders dashboard skeleton with stats and cards', () => {
      render(<SkeletonDashboard />);
      expect(screen.getAllByTestId('skeleton-stat-card')).toHaveLength(4);
    });
  });

  describe('SkeletonForm', () => {
    it('renders default 4 fields', () => {
      render(<SkeletonForm />);
      const fieldGroups = screen.getAllByTestId('skeleton-form-field');
      expect(fieldGroups.length).toBeGreaterThanOrEqual(4);
    });

    it('renders custom number of fields', () => {
      render(<SkeletonForm fields={6} />);
      const fieldGroups = screen.getAllByTestId('skeleton-form-field');
      expect(fieldGroups.length).toBeGreaterThanOrEqual(6);
    });
  });

  describe('SkeletonDetail', () => {
    it('renders detail page skeleton', () => {
      render(<SkeletonDetail />);
      expect(screen.getByTestId('skeleton-detail')).toBeInTheDocument();
    });
  });

  describe('SkeletonList', () => {
    it('renders default 5 items', () => {
      render(<SkeletonList />);
      expect(screen.getAllByTestId('skeleton-list-item')).toHaveLength(5);
    });

    it('renders custom number of items', () => {
      render(<SkeletonList items={3} />);
      expect(screen.getAllByTestId('skeleton-list-item')).toHaveLength(3);
    });
  });

  describe('SkeletonListItem', () => {
    it('renders list item skeleton', () => {
      render(<SkeletonListItem />);
      expect(screen.getByTestId('skeleton-list-item')).toHaveClass('animate-pulse');
      const skeletons = screen.getAllByTestId('skeleton');
      expect(skeletons.some((el) => el.classList.contains('rounded-full'))).toBe(true);
    });
  });

  describe('Spinner', () => {
    it('renders medium size by default', () => {
      render(<Spinner />);
      expect(screen.getByRole('status', { name: /loading/i })).toHaveClass('h-6', 'w-6', 'animate-spin');
    });

    it('renders small size', () => {
      render(<Spinner size="sm" />);
      expect(screen.getByRole('status', { name: /loading/i })).toHaveClass('h-4', 'w-4');
    });

    it('renders large size', () => {
      render(<Spinner size="lg" />);
      expect(screen.getByRole('status', { name: /loading/i })).toHaveClass('h-8', 'w-8');
    });

    it('applies custom className', () => {
      render(<Spinner className="text-blue-500" />);
      expect(screen.getByRole('status', { name: /loading/i })).toHaveClass('text-blue-500');
    });
  });

  describe('LoadingOverlay', () => {
    it('renders with default message', () => {
      render(<LoadingOverlay />);
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    it('renders with custom message', () => {
      render(<LoadingOverlay message="Please wait..." />);
      expect(screen.getByText('Please wait...')).toBeInTheDocument();
    });

    it('has fixed positioning', () => {
      render(<LoadingOverlay />);
      expect(screen.getByTestId('loading-overlay')).toHaveClass('fixed', 'inset-0');
    });
  });

  describe('LoadingInline', () => {
    it('renders with default message', () => {
      render(<LoadingInline />);
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    it('renders with custom message', () => {
      render(<LoadingInline message="Fetching data..." />);
      expect(screen.getByText('Fetching data...')).toBeInTheDocument();
    });

    it('includes spinner', () => {
      render(<LoadingInline />);
      expect(screen.getByRole('status', { name: /loading/i })).toBeInTheDocument();
    });
  });
});
