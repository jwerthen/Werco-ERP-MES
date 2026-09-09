import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import StockExclusions from './StockExclusions';
import { NestingPortalContext } from './PortalContext';
import type { StockExclusion } from './lib/stock-exclusions';

test('editing only the reason preserves the exact source outline and entered clearance', async () => {
  const region: StockExclusion = {
    id: 'exact-source',
    label: 'Synthetic reported area',
    reason: 'Initial estimator reason',
    clearance: 0.1,
    outline: { type: 'circle', cx: 12.3456789, cy: 11.23456789, r: 4.123456789 },
  };
  const changed = jest.fn<void, [StockExclusion[]]>();
  render(
    <NestingPortalContext.Provider value={document.body}>
      <StockExclusions
        option={{ id: 'sheet', width: 100, height: 100, price: null, enabled: true, exclusions: [region] }}
        onChange={changed}
      />
    </NestingPortalContext.Provider>
  );
  fireEvent.click(screen.getByRole('button', { name: /^Excluded areas for/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit excluded area Synthetic reported area' }));
  fireEvent.change(screen.getByLabelText('Reason this material is unavailable'), {
    target: { value: 'Reason clarified' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Update excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0][0]).toEqual({ ...region, reason: 'Reason clarified' });
  expect(changed.mock.calls[0][0][0].outline).toBe(region.outline);
});

test('a rounded display and radius-only edit preserve untouched center coordinates and clearance exactly', async () => {
  const outline = { type: 'circle' as const, cx: 152.39999999999998, cy: 133.7123456789123, r: 4.123456789 };
  const region: StockExclusion = {
    id: 'radius-edit',
    label: 'Synthetic circular defect',
    reason: 'Initial estimator reason',
    clearance: 0.1,
    outline,
  };
  const changed = jest.fn<void, [StockExclusion[]]>();
  render(
    <NestingPortalContext.Provider value={document.body}>
      <StockExclusions
        option={{ id: 'sheet', width: 254, height: 254, price: null, enabled: true, exclusions: [region] }}
        onChange={changed}
      />
    </NestingPortalContext.Provider>
  );
  fireEvent.click(screen.getByRole('button', { name: /^Excluded areas for/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit excluded area Synthetic circular defect' }));
  expect(screen.getByLabelText('Center X (in)')).toHaveValue('6');
  fireEvent.change(screen.getByLabelText('Radius (in)'), { target: { value: '1/4' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0][0]).toEqual({ ...region, outline: { ...outline, r: 6.35 } });
  expect(outline.r).toBe(4.123456789);
});
