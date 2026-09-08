import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { rect, type Part } from './lib/nesting';
import { demoQuote, quoteFromFile, quoteToFile } from './lib/quoting';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));

function reopenedQuote(mode?: 'drawing-bounds') {
  const part: Part = {
    ...demoQuote.parts[0],
    name: 'Imported drawing',
    quantity: 1,
    loops: [rect(100, 50)],
    importMode: mode,
  };
  return quoteFromFile(JSON.parse(JSON.stringify(quoteToFile({ ...demoQuote, parts: [part] }))));
}

describe('saved DXF geometry basis in the quoting workspace', () => {
  it('identifies legacy rectangular footprints and requires re-import before nesting', () => {
    render(<NestingWorkspace initialQuote={reopenedQuote('drawing-bounds')} />);
    expect(screen.getByText('Legacy footprint · re-import DXF')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Compare sheets' })).toBeDisabled();
    expect(
      screen.getAllByText(/Remove those parts and re-import their DXFs to nest actual contours/).length
    ).toBeGreaterThan(0);
    expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
  });

  it('uses net part area for saved contours after an explicit comparison', async () => {
    render(<NestingWorkspace initialQuote={reopenedQuote()} />);
    expect(screen.queryByText('Legacy footprint · re-import DXF')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    expect(await screen.findByText('Net part area', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('Estimated footprint area')).not.toBeInTheDocument();
  });
});
