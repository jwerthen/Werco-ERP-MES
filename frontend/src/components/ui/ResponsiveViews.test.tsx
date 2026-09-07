import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ResponsiveViews } from './ResponsiveViews';

const originalWidth = window.innerWidth;
afterEach(() => {
  Object.defineProperty(window, 'innerWidth', { value: originalWidth, configurable: true });
});
function resize(width: number) {
  act(() => {
    Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
    window.dispatchEvent(new Event('resize'));
  });
}

it('mounts only the initial active view, including mobile before the first effect', () => {
  resize(390);
  const desktopRender = jest.fn(() => <p>Desktop table</p>);
  const mobileRender = jest.fn(() => <p>Mobile cards</p>);
  render(<ResponsiveViews desktop={React.createElement(desktopRender)} mobile={React.createElement(mobileRender)} />);
  expect(desktopRender).not.toHaveBeenCalled();
  expect(mobileRender).toHaveBeenCalled();
  expect(screen.queryByText('Desktop table')).not.toBeInTheDocument();
  resize(1024);
  expect(screen.getByText('Desktop table')).toBeInTheDocument();
  expect(screen.queryByText('Mobile cards')).not.toBeInTheDocument();
});

it('preserves parent-owned filter and sort controls across both breakpoint directions', () => {
  resize(1440);
  function ControlledList() {
    const [filter, setFilter] = useState('Customer A');
    const [sort, setSort] = useState('Due date');
    return (
      <>
        <input aria-label="Filter" value={filter} onChange={event => setFilter(event.target.value)} />
        <button onClick={() => setSort('Priority')}>Sort by priority</button>
        <ResponsiveViews
          desktop={
            <p>
              Table: {filter}, {sort}
            </p>
          }
          mobile={
            <p>
              Cards: {filter}, {sort}
            </p>
          }
        />
      </>
    );
  }
  render(<ControlledList />);
  fireEvent.change(screen.getByRole('textbox', { name: 'Filter' }), { target: { value: 'Customer B' } });
  fireEvent.click(screen.getByRole('button', { name: 'Sort by priority' }));
  resize(1023);
  expect(screen.getByText('Cards: Customer B, Priority')).toBeInTheDocument();
  expect(screen.queryByText(/Table:/)).not.toBeInTheDocument();
  resize(1024);
  expect(screen.getByText('Table: Customer B, Priority')).toBeInTheDocument();
  expect(screen.queryByText(/Cards:/)).not.toBeInTheDocument();
});
