import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { rect, type Part } from './lib/nesting';
import { demoQuote, quoteFromFile, quoteToFile } from './lib/quoting';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));

describe('saved DXF geometry basis in the quoting workspace', () => {
  it.each([
    { mode: 'drawing-bounds' as const, areaLabel: 'Estimated footprint area' },
    { mode: undefined, areaLabel: 'Net part area' },
  ])('uses $areaLabel after reopening and comparing an estimate', async ({ mode, areaLabel }) => {
    const part: Part = {
      ...demoQuote.parts[0],
      name: 'Imported drawing',
      quantity: 1,
      loops: [rect(100, 50)],
      importMode: mode,
    };
    const reopened = quoteFromFile(JSON.parse(JSON.stringify(quoteToFile({ ...demoQuote, parts: [part] }))));
    render(<NestingWorkspace initialQuote={reopened} />);
    const basisNote = screen.queryByText('Whole drawing footprint · verify size');
    if (mode === 'drawing-bounds') expect(basisNote).toBeInTheDocument();
    else expect(basisNote).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
    expect(await screen.findByText(areaLabel, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(
      screen.queryByText(mode === 'drawing-bounds' ? 'Net part area' : 'Estimated footprint area')
    ).not.toBeInTheDocument();
  });
});
