import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { remnantStageFixture } from '../../test-utils/remnantStageFixtures';
import { projectFromFile } from './lib/quote-project';
import RemnantPlanningResults from './RemnantPlanningResults';
test('conditional counts remain alternative-local and stale results cannot expose old geometry', async () => {
  const f = await remnantStageFixture();
  const props = { project: projectFromFile(f.raw), rawEstimate: f.raw, stages: f.stages, status: 'Search finished' };
  const view = render(<RemnantPlanningResults {...props} stale={false} />);
  expect(screen.getByText(/1 reported piece \+ 1 full sheets/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'View remaining sheets' }));
  expect(screen.getByRole('img')).toHaveTextContent('original instance 2');
  view.rerender(<RemnantPlanningResults {...props} stale />);
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'View remaining sheets' })).toBeDisabled();
});

test('SVG download embeds physical inch dimensions and review constraints without changing its viewBox', async () => {
  const f = await remnantStageFixture();
  const create = jest.spyOn(URL, 'createObjectURL');
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  try {
    render(
      <RemnantPlanningResults
        project={projectFromFile(f.raw)}
        rawEstimate={f.raw}
        stages={f.stages}
        status="Finished"
        stale={false}
      />
    );
    const drawing = screen.getByRole('img');
    fireEvent.click(screen.getByRole('button', { name: 'Download layout SVG' }));
    const blob = create.mock.calls[0][0];
    if (!(blob instanceof Blob)) throw new Error('No SVG blob');
    let text = '';
    await act(async () => {
      text = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsText(blob);
      });
    });
    const exported = new DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
    expect(exported.tagName).toBe('svg');
    expect(exported.getAttribute('width')).toMatch(/in$/);
    expect(exported.getAttribute('height')).toMatch(/in$/);
    expect(parseFloat(exported.getAttribute('width')!)).toBeCloseTo(3, 12);
    expect(parseFloat(exported.getAttribute('height')!)).toBeCloseTo(3, 12);
    expect(exported.getAttribute('viewBox')).toBe(drawing.getAttribute('viewBox'));
    expect(exported.querySelector('title')?.textContent).toMatch(/margin 0.125 in; part gap 0.125 in/);
    expect(exported.querySelector('title')?.textContent).toContain('Planning review only');
  } finally {
    create.mockRestore();
    click.mockRestore();
  }
});
