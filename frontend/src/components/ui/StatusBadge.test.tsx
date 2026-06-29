/**
 * StatusBadge Component Tests
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { StatusBadge } from './StatusBadge';
import { variantClass } from '../../utils/statusColors';

describe('StatusBadge', () => {
  it('renders the humanized status label', () => {
    render(<StatusBadge status="in_progress" />);
    expect(screen.getByText('in progress')).toBeInTheDocument();
  });

  describe('default coloring (central statusColors map)', () => {
    it('colors in_progress blue from the central map', () => {
      render(<StatusBadge status="in_progress" />);
      const badge = screen.getByText('in progress');
      variantClass.blue.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });

    it('colors released green from the central map', () => {
      render(<StatusBadge status="released" />);
      const badge = screen.getByText('released');
      variantClass.green.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });

    it('colors cancelled red from the central map', () => {
      render(<StatusBadge status="cancelled" />);
      const badge = screen.getByText('cancelled');
      variantClass.red.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });

    it('falls back to slate for an unknown status', () => {
      render(<StatusBadge status="made_up" />);
      const badge = screen.getByText('made up');
      variantClass.slate.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });
  });

  describe('colorMap override', () => {
    it('uses the provided colorMap instead of the central map', () => {
      render(
        <StatusBadge
          status="in_progress"
          colorMap={{ in_progress: 'bg-purple-500/20 text-purple-300' }}
        />,
      );
      const badge = screen.getByText('in progress');
      expect(badge).toHaveClass('bg-purple-500/20', 'text-purple-300');
      // central map's blue must NOT leak through when an override is supplied
      expect(badge).not.toHaveClass('text-blue-300');
    });

    it('falls back to slate when a status is missing from the colorMap override', () => {
      render(<StatusBadge status="weird" colorMap={{ other: 'bg-pink-500/20 text-pink-300' }} />);
      const badge = screen.getByText('weird');
      variantClass.slate.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });
  });

  it('applies custom className', () => {
    render(<StatusBadge status="active" className="custom-x" />);
    expect(screen.getByText('active')).toHaveClass('custom-x');
  });
});
