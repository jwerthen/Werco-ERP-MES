/**
 * Skeleton Components Tests
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
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
      const { container } = render(<Skeleton />);
      expect(container.firstChild).toHaveClass('animate-pulse', 'bg-gray-200', 'rounded');
    });

    it('applies custom className', () => {
      const { container } = render(<Skeleton className="h-4 w-full" />);
      expect(container.firstChild).toHaveClass('h-4', 'w-full');
    });

    it('applies custom width and height', () => {
      const { container } = render(<Skeleton width={100} height={50} />);
      expect(container.firstChild).toHaveStyle({ width: '100px', height: '50px' });
    });

    it('applies string width and height', () => {
      const { container } = render(<Skeleton width="50%" height="2rem" />);
      expect(container.firstChild).toHaveStyle({ width: '50%', height: '2rem' });
    });
  });

  describe('SkeletonText', () => {
    it('renders single line by default', () => {
      const { container } = render(<SkeletonText />);
      expect(container.querySelectorAll('.animate-pulse')).toHaveLength(1);
    });

    it('renders multiple lines', () => {
      const { container } = render(<SkeletonText lines={3} />);
      expect(container.querySelectorAll('.animate-pulse')).toHaveLength(3);
    });

    it('last line is shorter when multiple lines', () => {
      const { container } = render(<SkeletonText lines={3} />);
      const skeletons = container.querySelectorAll('.animate-pulse');
      expect(skeletons[2]).toHaveClass('w-3/4');
    });
  });

  describe('SkeletonAvatar', () => {
    it('renders medium size by default', () => {
      const { container } = render(<SkeletonAvatar />);
      expect(container.firstChild).toHaveClass('h-10', 'w-10', 'rounded-full');
    });

    it('renders small size', () => {
      const { container } = render(<SkeletonAvatar size="sm" />);
      expect(container.firstChild).toHaveClass('h-8', 'w-8');
    });

    it('renders large size', () => {
      const { container } = render(<SkeletonAvatar size="lg" />);
      expect(container.firstChild).toHaveClass('h-12', 'w-12');
    });
  });

  describe('SkeletonButton', () => {
    it('renders with default width', () => {
      const { container } = render(<SkeletonButton />);
      expect(container.firstChild).toHaveClass('h-9', 'w-24', 'rounded-md');
    });

    it('renders with custom width', () => {
      const { container } = render(<SkeletonButton width="w-32" />);
      expect(container.firstChild).toHaveClass('w-32');
    });
  });

  describe('SkeletonBadge', () => {
    it('renders badge skeleton', () => {
      const { container } = render(<SkeletonBadge />);
      expect(container.firstChild).toHaveClass('h-6', 'w-16', 'rounded-full');
    });
  });

  describe('SkeletonCard', () => {
    it('renders card with skeleton content', () => {
      const { container } = render(<SkeletonCard />);
      expect(container.querySelector('.bg-white')).toBeInTheDocument();
      expect(container.querySelector('.rounded-lg')).toBeInTheDocument();
    });

    it('applies custom className', () => {
      const { container } = render(<SkeletonCard className="h-80" />);
      expect(container.firstChild).toHaveClass('h-80');
    });
  });

  describe('SkeletonTable', () => {
    it('renders default rows and columns', () => {
      const { container } = render(<SkeletonTable />);
      // Default is 5 rows, 6 columns
      expect(container.querySelectorAll('tbody tr')).toHaveLength(5);
    });

    it('renders custom rows and columns', () => {
      const { container } = render(<SkeletonTable rows={3} columns={4} />);
      expect(container.querySelectorAll('tbody tr')).toHaveLength(3);
      expect(container.querySelectorAll('tbody tr:first-child td')).toHaveLength(4);
    });

    it('hides header when showHeader is false', () => {
      const { container } = render(<SkeletonTable showHeader={false} />);
      expect(container.querySelector('thead')).not.toBeInTheDocument();
    });
  });

  describe('SkeletonTableRow', () => {
    it('renders correct number of columns', () => {
      const { container } = render(
        <table><tbody><SkeletonTableRow columns={5} /></tbody></table>
      );
      expect(container.querySelectorAll('td')).toHaveLength(5);
    });
  });

  describe('SkeletonStatCard', () => {
    it('renders stat card skeleton', () => {
      const { container } = render(<SkeletonStatCard />);
      expect(container.querySelector('.bg-white')).toBeInTheDocument();
      expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
    });
  });

  describe('SkeletonDashboard', () => {
    it('renders dashboard skeleton with stats and cards', () => {
      const { container } = render(<SkeletonDashboard />);
      // Should have 4 stat cards
      expect(container.querySelectorAll('.bg-white.rounded-lg.shadow.p-6.animate-pulse')).toHaveLength(4);
    });
  });

  describe('SkeletonForm', () => {
    it('renders default 4 fields', () => {
      const { container } = render(<SkeletonForm />);
      const fieldGroups = container.querySelectorAll('.space-y-2');
      expect(fieldGroups.length).toBeGreaterThanOrEqual(4);
    });

    it('renders custom number of fields', () => {
      const { container } = render(<SkeletonForm fields={6} />);
      const fieldGroups = container.querySelectorAll('.space-y-2');
      expect(fieldGroups.length).toBeGreaterThanOrEqual(6);
    });
  });

  describe('SkeletonDetail', () => {
    it('renders detail page skeleton', () => {
      const { container } = render(<SkeletonDetail />);
      expect(container.querySelector('.space-y-6')).toBeInTheDocument();
    });
  });

  describe('SkeletonList', () => {
    it('renders default 5 items', () => {
      const { container } = render(<SkeletonList />);
      expect(container.querySelectorAll('.flex.items-center.gap-4')).toHaveLength(5);
    });

    it('renders custom number of items', () => {
      const { container } = render(<SkeletonList items={3} />);
      expect(container.querySelectorAll('.flex.items-center.gap-4')).toHaveLength(3);
    });
  });

  describe('SkeletonListItem', () => {
    it('renders list item skeleton', () => {
      const { container } = render(<SkeletonListItem />);
      expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
      expect(container.querySelector('.rounded-full')).toBeInTheDocument(); // Avatar
    });
  });

  describe('Spinner', () => {
    it('renders medium size by default', () => {
      const { container } = render(<Spinner />);
      expect(container.firstChild).toHaveClass('h-6', 'w-6', 'animate-spin');
    });

    it('renders small size', () => {
      const { container } = render(<Spinner size="sm" />);
      expect(container.firstChild).toHaveClass('h-4', 'w-4');
    });

    it('renders large size', () => {
      const { container } = render(<Spinner size="lg" />);
      expect(container.firstChild).toHaveClass('h-8', 'w-8');
    });

    it('applies custom className', () => {
      const { container } = render(<Spinner className="text-blue-500" />);
      expect(container.firstChild).toHaveClass('text-blue-500');
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
      const { container } = render(<LoadingOverlay />);
      expect(container.firstChild).toHaveClass('fixed', 'inset-0');
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
      const { container } = render(<LoadingInline />);
      expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    });
  });
});
