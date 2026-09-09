import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NestingWorkspace from './NestingWorkspace';
import { NestingPortalContext } from './PortalContext';
import { createBlankQuote } from './lib/quoting';
import { rect } from './lib/nesting';
import api from '../../services/api';
const mockToast = jest.fn();
jest.mock('../../components/ui/Toast', () => ({ useToast: () => ({ showToast: mockToast }) }));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingMaterials: jest.fn().mockResolvedValue({ schema_version: 1, items: [], total: 0, offset: 0, limit: 200 }),
    generateNestingBuyerPdf: jest.fn(),
  },
}));
const quote = () => ({
  ...createBlankQuote(),
  margin: 3.175,
  gap: 3.175,
  parts: [{ id: 'p1', name: 'Plate', quantity: 2, rotate: true, color: 0, loops: [rect(25.4, 25.4)] }],
  options: [
    { id: 'complete', width: 101.6, height: 101.6, enabled: true, price: null },
    { id: 'too-small', width: 12.7, height: 12.7, enabled: true, price: null },
  ],
});
test('buyer export starts disabled, enables for a complete comparison and invalidates on edit and New', async () => {
  await act(async () => {
    render(
      <NestingPortalContext.Provider value={document.body}>
        <NestingWorkspace initialQuote={quote()} companyId={2} estimatorId={7} canExportBuyerPdf />
      </NestingPortalContext.Provider>
    );
  });
  const exportButton = screen.getByRole('button', { name: 'Export buyer PDF' });
  expect(exportButton).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await waitFor(() => expect(exportButton).toBeEnabled());
  fireEvent.click(exportButton);
  await screen.findByRole('dialog', { name: 'Buyer material plan PDF' });
  expect(screen.getAllByRole('option')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: 'Close report' }));
  fireEvent.change(screen.getByLabelText('Quantity for Plate'), { target: { value: '3' } });
  expect(exportButton).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Compare sheets' }));
  await waitFor(() => expect(exportButton).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'New' }));
  expect(exportButton).toBeDisabled();
  expect(api.generateNestingBuyerPdf).not.toHaveBeenCalled();
});
