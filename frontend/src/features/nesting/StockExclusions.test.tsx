import React, { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import StockExclusions, { stockExclusionsSvg } from './StockExclusions';
import { NestingPortalContext } from './PortalContext';
import type { SheetOption } from './lib/quoting';
import type { StockExclusion } from './lib/stock-exclusions';
import { inToMm } from './lib/units';

const option: SheetOption = { id: 'test', width: 254, height: 254, enabled: true, price: 100 };
function mount(regions?: StockExclusion[], onChange = jest.fn()) {
  function Harness() {
    const [exclusions, setExclusions] = useState(regions);
    return (
      <NestingPortalContext.Provider value={document.body}>
        <StockExclusions
          option={{ ...option, exclusions }}
          onChange={next => {
            onChange(next);
            setExclusions(next);
          }}
        />
      </NestingPortalContext.Provider>
    );
  }
  render(<Harness />);
  fireEvent.click(screen.getByRole('button', { name: /^Excluded areas for/ }));
  return onChange;
}
function field(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

test('adds exact fraction-inch stock geometry with a required reason, edits it, then explicitly removes it', async () => {
  const changed = mount();
  expect(screen.getByText(/repeat on every hypothetical sheet/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'New excluded area' }));
  field('Area label', 'Edge defect');
  fireEvent.click(screen.getByRole('button', { name: 'Add excluded area' }));
  await screen.findByText('Explain why this material is unavailable.');
  expect(changed).not.toHaveBeenCalled();
  field('Reason this material is unavailable', 'Reported by material buyer');
  field('Left X (in)', '1/2');
  field('Bottom Y (in)', '1');
  field('Size along X (in)', '2');
  field('Size along Y (in)', '1 1/2');
  field('Added clearance (in)', '1/8');
  fireEvent.click(screen.getByRole('button', { name: 'Add excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0][0]).toMatchObject({
    label: 'Edge defect',
    reason: 'Reported by material buyer',
    clearance: inToMm(0.125),
  });
  const points = changed.mock.calls[0][0][0].outline.points;
  [
    [12.7, 25.4],
    [63.5, 25.4],
    [63.5, 63.5],
    [12.7, 63.5],
  ].forEach(([x, y], index) => {
    expect(points[index].x).toBeCloseTo(x, 12);
    expect(points[index].y).toBeCloseTo(y, 12);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Edit excluded area Edge defect' }));
  field('Area label', 'Checked edge');
  fireEvent.click(screen.getByRole('button', { name: 'Update excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(2));
  expect(changed.mock.calls[1][0][0].outline).toBe(changed.mock.calls[0][0][0].outline);
  fireEvent.click(screen.getByRole('button', { name: 'Remove excluded area Checked edge' }));
  expect(changed.mock.calls[2][0]).toEqual([]);
});

test('rejects a circle outside the gross sheet without cropping it and accepts an exact interior circle', async () => {
  const changed = mount();
  fireEvent.click(screen.getByRole('button', { name: 'New excluded area' }));
  field('Area label', 'Spot');
  field('Reason this material is unavailable', 'Surface defect');
  field('Area shape', 'circle');
  field('Center X (in)', '9');
  field('Center Y (in)', '9');
  field('Radius (in)', '2');
  fireEvent.click(screen.getByRole('button', { name: 'Add excluded area' }));
  await screen.findByText(/must lie wholly inside the gross sheet/);
  expect(changed).not.toHaveBeenCalled();
  field('Radius (in)', '1/2');
  fireEvent.click(screen.getByRole('button', { name: 'Add excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0][0].outline).toEqual({ type: 'circle', cx: 228.6, cy: 228.6, r: 12.7 });
});

test('preserves an irregular polygon byte-for-byte when only metadata or clearance changes', async () => {
  const region: StockExclusion = {
    id: 'irregular',
    label: 'Imported area',
    reason: 'Reviewed source outline',
    clearance: 0,
    outline: {
      type: 'poly',
      points: [
        { x: 10.123456789, y: 5 },
        { x: 45.987654321, y: 5 },
        { x: 20, y: 32.456789123 },
      ],
    },
  };
  const changed = mount([region]);
  fireEvent.click(screen.getByRole('button', { name: 'Edit excluded area Imported area' }));
  expect(screen.getByLabelText('Area shape')).toBeDisabled();
  expect(screen.queryByLabelText('Left X (in)')).not.toBeInTheDocument();
  field('Reason this material is unavailable', 'Reason updated after review');
  field('Added clearance (in)', '1/16');
  fireEvent.click(screen.getByRole('button', { name: 'Update excluded area' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0][0].outline).toBe(region.outline);
  expect(changed.mock.calls[0][0][0].clearance).toBe(1.5875);
});

test('escapes user-entered area labels and reasons in standalone SVG exports', () => {
  const output = stockExclusionsSvg([
    {
      id: 'safe',
      label: '<script>alert(1)</script>',
      reason: 'A&B "quoted"',
      outline: { type: 'circle', cx: 25.4, cy: 25.4, r: 12.7 },
      clearance: 0,
    },
  ]);
  expect(output).not.toContain('<script>');
  expect(output).toContain('&lt;script&gt;');
  expect(output).toContain('A&amp;B &quot;quoted&quot;');
});
