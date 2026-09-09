import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { CURRENT_GEOMETRY_PROFILE } from './lib/geometry-profile';
import { rect } from './lib/nesting';
import { createBlankQuote } from './lib/quoting';
import * as workerClient from './lib/nesting-worker-client';

const mockShowToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockShowToast }) }));
afterEach(() => {
  jest.restoreAllMocks();
  mockShowToast.mockClear();
});

test('opening older inputs waits for an explicit clearance upgrade and preserves their geometry and prices', async () => {
  const quote = createBlankQuote();
  delete quote.geometryProfile;
  quote.margin = 2;
  quote.gap = 1;
  quote.parts = [
    { id: 'plate', name: 'Synthetic old plate', quantity: 1, rotate: false, color: 0, loops: [rect(10, 20)] },
  ];
  quote.options = [{ id: 'sheet', width: 100, height: 100, price: 35, enabled: true }];
  const before = JSON.stringify(quote);
  const compare = jest.spyOn(workerClient, 'compareSheetsInWorker');
  render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace initialQuote={quote} />
    </NestingPortalContext.Provider>
  );
  expect(compare).not.toHaveBeenCalled();
  expect(screen.getByRole('region', { name: 'Clearance rules update' })).toHaveTextContent('more sheets');
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await waitFor(() =>
    expect(mockShowToast).toHaveBeenCalledWith('error', expect.stringMatching(/earlier clearance rules/))
  );
  expect(compare).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Use current clearance rules' }));
  expect(screen.queryByRole('region', { name: 'Clearance rules update' })).not.toBeInTheDocument();
  expect(compare).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await screen.findByRole('img', { name: /^Layout on / });
  const sent = compare.mock.calls[0][0];
  expect(sent.geometryProfile).toEqual(CURRENT_GEOMETRY_PROFILE);
  const { geometryProfile, ...preserved } = sent;
  void geometryProfile;
  expect(preserved).toEqual(quote);
  expect(JSON.stringify(quote)).toBe(before);
});

test('fresh entry stays empty and uses current rules without restoring a demo or prior nest', () => {
  render(
    <NestingPortalContext.Provider value={document.body}>
      <NestingWorkspace />
    </NestingPortalContext.Provider>
  );
  expect(screen.queryByRole('button', { name: 'Use current clearance rules' })).not.toBeInTheDocument();
  expect(screen.queryByRole('img', { name: /^Layout on / })).not.toBeInTheDocument();
  expect(screen.getByText('Compensated clearance envelopes')).toBeInTheDocument();
});
