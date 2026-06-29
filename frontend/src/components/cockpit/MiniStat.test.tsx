/**
 * Shared cockpit MiniStat — public API regression.
 *
 * MiniStat is the extracted compact KPI tile reused across pages (replacing the
 * bulky big-stat-icon cards). It renders as a static tile, a <Link> (href), or a
 * filter <button> (onClick) with an active state. This locks those three modes.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { CubeIcon } from '@heroicons/react/24/outline';
import { MiniStat } from './MiniStat';

const renderIn = (ui: React.ReactElement) => render(<MemoryRouter>{ui}</MemoryRouter>);

test('renders label and value as a static tile (no link/button)', () => {
  renderIn(<MiniStat icon={CubeIcon} iconBg="bg-fd-green/15" iconColor="text-fd-green" label="Open NCRs" value={7} />);
  expect(screen.getByText('Open NCRs')).toBeInTheDocument();
  expect(screen.getByText('7')).toBeInTheDocument();
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.queryByRole('button')).toBeNull();
});

test('renders as a link when href is set', () => {
  renderIn(<MiniStat icon={CubeIcon} iconBg="x" iconColor="y" label="Low Stock" value={3} href="/inventory" />);
  expect(screen.getByRole('link')).toHaveAttribute('href', '/inventory');
});

test('renders as a filter button when onClick is set, reflecting active state', () => {
  const onClick = jest.fn();
  renderIn(<MiniStat icon={CubeIcon} iconBg="x" iconColor="y" label="Overdue" value={2} onClick={onClick} active />);
  const btn = screen.getByRole('button');
  expect(btn).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(btn);
  expect(onClick).toHaveBeenCalledTimes(1);
});
