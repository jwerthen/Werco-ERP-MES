/**
 * The printed purchase order's supplier quality flow-down block (AS9100D 8.4.3).
 *
 * `quality_flowdown` on the print data is the block the PO carries — live on a draft, frozen
 * once issued (the server decides which). The page prints it between the lines table and the
 * notes: the heading, "Reference: …", the standard paragraphs, then one entry per clause. It
 * prints nothing for `null`, a missing key (an API that predates the field) or a disabled
 * block. Every value is supplier-facing document text, so it renders as text, never markup.
 *
 * Clause codes and wording are invented.
 */
import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import PrintPurchaseOrder from './PrintPurchaseOrder';
import type { POFlowdownPrint } from '../types/poFlowdown';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getPurchaseOrderPrintData: jest.fn() },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const BLOCK: POFlowdownPrint = {
  enabled: true,
  heading: 'Supplier Quality Terms (test)',
  document_reference: 'SQR-TEST Rev B',
  body: 'First test paragraph.\n\nSecond test paragraph,\nwith a kept line break.',
  clauses: [
    { code: 'QX3', title: 'Charlie requirement', body: null },
    { code: 'QX1', title: 'Alpha requirement', body: 'Alpha clause text.' },
  ],
};

function printData(quality_flowdown?: POFlowdownPrint | null) {
  return {
    po_number: 'PO-FD-0001',
    status: 'sent',
    vendor_name: 'Test Vendor',
    vendor_address: '1 Test Way',
    order_date: '11/19/2026',
    subtotal: '$7.00',
    tax: '$0.00',
    shipping: '$0.00',
    total: '$7.00',
    notes: 'Closing test notes.',
    lines: [
      {
        line_number: 1,
        part_number: 'PFD-1',
        part_name: 'Test part',
        quantity_ordered: '2',
        quantity_received: '0',
        unit_price: '$3.50',
        line_total: '$7.00',
        required_date: null,
      },
    ],
    printed_at: '11/19/2026',
    ...(quality_flowdown === undefined ? {} : { quality_flowdown }),
  };
}

async function renderPrint(data: ReturnType<typeof printData>) {
  mockedApi.getPurchaseOrderPrintData.mockResolvedValue(data as any);
  render(
    <MemoryRouter initialEntries={['/print/purchase-order/7']}>
      <Routes>
        <Route path="/print/purchase-order/:id" element={<PrintPurchaseOrder />} />
      </Routes>
    </MemoryRouter>
  );
  // The page has loaded once the PO number is on it.
  await screen.findByText('PO-FD-0001');
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('PrintPurchaseOrder — quality flow-down block', () => {
  test('prints the heading, reference, paragraphs and clauses between the lines and the notes', async () => {
    await renderPrint(printData(BLOCK));

    expect(mockedApi.getPurchaseOrderPrintData).toHaveBeenCalledWith(7);
    const block = screen.getByRole('region', { name: 'Supplier Quality Terms (test)' });
    expect(within(block).getByRole('heading', { name: 'Supplier Quality Terms (test)' })).toBeInTheDocument();
    expect(within(block).getByText('Reference: SQR-TEST Rev B')).toBeInTheDocument();
    // Paragraphs split on a blank line; a single line break stays inside its paragraph.
    expect(within(block).getByText('First test paragraph.')).toBeInTheDocument();
    expect(
      within(block).getByText(
        (_, element) => element?.textContent === 'Second test paragraph,\nwith a kept line break.'
      )
    ).toBeInTheDocument();

    const clauses = within(block).getAllByRole('listitem');
    expect(clauses.map(item => item.textContent)).toEqual([
      'QX3 — Charlie requirement',
      'QX1 — Alpha requirementAlpha clause text.',
    ]);

    // Between the lines table and the notes.
    const linesTable = screen.getByText('PFD-1').closest('table') as HTMLTableElement;
    const notes = screen.getByText('Closing test notes.');
    expect(linesTable.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(block.compareDocumentPosition(notes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  test('renders markup in the stored text as text, never as elements', async () => {
    await renderPrint(
      printData({
        enabled: true,
        heading: 'Terms <REF> & Co',
        document_reference: 'SQR<1>',
        body: 'Check OD<ID before press. <b>Not bold</b>',
        clauses: [{ code: 'QX1', title: 'Cert <TYP>', body: '<script>alert(1)</script>' }],
      })
    );

    const block = screen.getByTestId('po-quality-flowdown');
    expect(block).toHaveTextContent('Terms <REF> & Co');
    expect(block).toHaveTextContent('Reference: SQR<1>');
    expect(block).toHaveTextContent('Check OD<ID before press. <b>Not bold</b>');
    expect(block).toHaveTextContent('QX1 — Cert <TYP>');
    expect(block).toHaveTextContent('<script>alert(1)</script>');
    expect(block.querySelector('b, script, ref, typ')).toBeNull();
  });

  test('a blank heading falls back to the default; no reference, body or clauses prints just the heading', async () => {
    await renderPrint(printData({ enabled: true, heading: '  ', document_reference: null, body: null, clauses: [] }));

    const block = screen.getByTestId('po-quality-flowdown');
    expect(within(block).getByRole('heading', { name: 'Supplier Quality Requirements' })).toBeInTheDocument();
    expect(within(block).queryByText(/^Reference:/)).toBeNull();
    expect(within(block).queryByRole('list')).toBeNull();
  });

  test.each([
    ['null (flow-down disabled, or issued before it existed)', null],
    ['a disabled block', { enabled: false } as POFlowdownPrint],
    ['an API that predates the field', undefined],
  ])('prints nothing for %s', async (_label, flowdown) => {
    await renderPrint(printData(flowdown));

    expect(screen.queryByTestId('po-quality-flowdown')).toBeNull();
    expect(screen.queryByText(/^Reference:/)).toBeNull();
    // The rest of the purchase order still prints.
    expect(screen.getByText('Closing test notes.')).toBeInTheDocument();
  });
});
