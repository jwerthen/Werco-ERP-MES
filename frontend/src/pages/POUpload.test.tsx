/**
 * Upload PO page — multi-file batch orchestration.
 *
 * The extraction endpoints are per-file and stateless; batch support is pure
 * frontend sequencing. These tests guard the queue behavior: multi-select with
 * per-file validation and removal, review-when-ready one-at-a-time sequencing
 * (the first READY doc in add order — a fast doc 2 is never blocked behind a
 * slow doc 1), failure isolation (one bad file never blocks the rest),
 * skip/retry, pool cancellation and stale-response guards, the end-of-batch
 * summary, and the shared currency/percent formatters in the review screen.
 */

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import POUpload from './POUpload';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    uploadPOPdf: jest.fn(),
    uploadQuotePdf: jest.fn(),
    createPOFromUpload: jest.fn(),
    searchPartsForPO: jest.fn(),
    searchVendorsForPO: jest.fn(),
    downloadPOPdf: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function pdfFile(name: string, content = '%PDF-1.4 stub') {
  return new File([content], name, { type: 'application/pdf' });
}

interface LineOverrides {
  [key: string]: unknown;
}

function makeLineItem(overrides: LineOverrides = {}) {
  return {
    line_number: 1,
    part_number: 'AN960-10L',
    description: 'Flat washer',
    qty_ordered: 10,
    unit_of_measure: 'EA',
    unit_price: 1.25,
    line_total: 12.5,
    confidence: 'high',
    suggested_part_type: 'hardware',
    suggested_part_number: null,
    part_match: { matched: true, match_id: 42, match_name: 'AN960-10L', confidence: 99, suggestions: [] },
    matched_part_id: 42,
    ...overrides,
  };
}

function makeExtraction(overrides: LineOverrides = {}) {
  return {
    document_type: 'po',
    po_number: 'PO-001',
    quote_number: null,
    vendor: { name: 'Acme Aerospace', address: '1 Hangar Way' },
    vendor_match: { matched: true, match_id: 5, match_name: 'Acme Aerospace', confidence: 95, suggestions: [] },
    matched_vendor_id: 5,
    order_date: '2026-07-01',
    expected_delivery_date: null,
    required_date: null,
    payment_terms: null,
    shipping_method: null,
    ship_to: null,
    line_items: [makeLineItem()],
    subtotal: null,
    tax: null,
    shipping_cost: null,
    total_amount: null,
    notes: null,
    extraction_confidence: 'high',
    pdf_was_ocr: false,
    pdf_page_count: 1,
    pdf_path: 'uploads/purchase_orders/stub.pdf',
    validation_issues: [],
    po_number_exists: false,
    ...overrides,
  };
}

function renderPOUpload() {
  return render(
    <MemoryRouter initialEntries={['/po-upload']}>
      <POUpload />
    </MemoryRouter>
  );
}

function chooseFiles(files: File[]) {
  fireEvent.change(screen.getByLabelText('Browse files'), { target: { files } });
}

function extractButton() {
  return screen.getByRole('button', { name: /extract data/i });
}

function createPOButton() {
  return screen.getByRole('button', { name: /create purchase order/i });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.createPOFromUpload.mockImplementation((data: any) =>
    Promise.resolve({ success: true, po_id: 101, po_number: data.po_number })
  );
});

describe('POUpload multi-file queue (upload step)', () => {
  it('lists multiple selected files, dedupes re-selection, and removes one via its icon button', () => {
    renderPOUpload();

    const one = pdfFile('po-1.pdf');
    const two = pdfFile('po-2.pdf', '%PDF-1.4 longer stub');
    chooseFiles([one, two]);

    expect(screen.getByText('po-1.pdf')).toBeInTheDocument();
    expect(screen.getByText('po-2.pdf')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Extract Data (2 files)' })).toBeInTheDocument();

    // Re-adding the same file (same name+size) does not duplicate it.
    fireEvent.change(screen.getByLabelText('Add more files'), { target: { files: [one] } });
    expect(screen.getAllByText('po-1.pdf')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Remove po-2.pdf' }));
    expect(screen.queryByText('po-2.pdf')).not.toBeInTheDocument();
    expect(screen.getByText('po-1.pdf')).toBeInTheDocument();
    // Single remaining file: the count suffix disappears.
    expect(screen.getByRole('button', { name: 'Extract Data' })).toBeInTheDocument();
  });

  it('rejects unsupported and oversize files with per-file messages while valid files still queue', () => {
    renderPOUpload();

    const good = pdfFile('good.pdf');
    const wrongType = new File(['plain text'], 'notes.txt', { type: 'text/plain' });
    const oversize = pdfFile('huge.pdf');
    Object.defineProperty(oversize, 'size', { value: 11 * 1024 * 1024 });

    chooseFiles([good, wrongType, oversize]);

    expect(screen.getByText(/notes\.txt: only PDF and Word documents/i)).toBeInTheDocument();
    expect(screen.getByText(/huge\.pdf: exceeds the 10MB limit/i)).toBeInTheDocument();
    // The valid file queued anyway; the rejected ones did not.
    expect(screen.getByText('good.pdf')).toBeInTheDocument();
    expect(screen.queryByText('notes.txt')).not.toBeInTheDocument();
    expect(screen.queryByText('huge.pdf')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Extract Data' })).toBeInTheDocument();
  });
});

describe('POUpload batch review sequencing', () => {
  it('reviews doc 1 first and loads doc 2 after Create PO succeeds, then shows the batch summary', async () => {
    mockedApi.uploadPOPdf.mockImplementation((file: File) =>
      Promise.resolve(makeExtraction({ po_number: file.name === 'po-1.pdf' ? 'PO-001' : 'PO-002' }))
    );

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf'), pdfFile('po-2.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    // Both extractions resolve immediately, so review-when-ready falls back
    // to add-order preference: po-1.pdf (first ready doc in add order) first.
    await screen.findByText(/Document 1 of 2/);
    expect(screen.getByText(/— po-1\.pdf/)).toBeInTheDocument();
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-001');
    expect(mockedApi.uploadPOPdf).toHaveBeenCalledTimes(2);

    fireEvent.click(createPOButton());

    // Doc 2's review loads with its own extraction data.
    await screen.findByText(/Document 2 of 2/);
    expect(screen.getByText(/— po-2\.pdf/)).toBeInTheDocument();
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-002');
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(1);
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledWith(
      expect.objectContaining({ po_number: 'PO-001', vendor_id: 5 })
    );

    fireEvent.click(createPOButton());

    // Queue exhausted: the summary lists both created POs.
    await screen.findByText('2 Purchase Orders Created!');
    expect(screen.getByText(/2 created/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'PO PO-001' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'PO PO-002' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to purchasing/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to receiving/i })).toBeInTheDocument();
  });

  it('a failed extraction does not block the other file, lands in the summary with its error, and can be retried', async () => {
    let failBadPdf = true;
    mockedApi.uploadPOPdf.mockImplementation((file: File) => {
      if (file.name === 'bad.pdf' && failBadPdf) {
        return Promise.reject({ response: { data: { detail: 'Could not parse document' } } });
      }
      return Promise.resolve(makeExtraction({ po_number: file.name === 'bad.pdf' ? 'PO-BAD' : 'PO-GOOD' }));
    });

    renderPOUpload();
    chooseFiles([pdfFile('bad.pdf'), pdfFile('good.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    // The good file (doc 2) reaches review even though doc 1 failed.
    await screen.findByText(/Document 2 of 2/);
    expect(screen.getByText(/— good\.pdf/)).toBeInTheDocument();
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-GOOD');

    fireEvent.click(createPOButton());

    // Summary: one created, one failed with its per-doc error and a Retry.
    await screen.findByText('Purchase Order Created!');
    expect(screen.getByText(/1 created/)).toBeInTheDocument();
    expect(screen.getByText(/1 failed/)).toBeInTheDocument();
    expect(screen.getByText('bad.pdf')).toBeInTheDocument();
    expect(screen.getByText('Could not parse document')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'PO PO-GOOD' })).toBeInTheDocument();

    // Retry re-queues the failed doc through the pool and returns to review.
    failBadPdf = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await screen.findByText(/Document 1 of 2/);
    expect(screen.getByText(/— bad\.pdf/)).toBeInTheDocument();
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-BAD');
  });

  it('Skip Document advances to the next document and skipped docs land in the summary', async () => {
    mockedApi.uploadPOPdf.mockImplementation(() => Promise.resolve(makeExtraction()));

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf'), pdfFile('po-2.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    await screen.findByText(/Document 1 of 2/);
    fireEvent.click(screen.getByRole('button', { name: /skip document/i }));

    await screen.findByText(/Document 2 of 2/);
    expect(screen.getByText(/— po-2\.pdf/)).toBeInTheDocument();
    expect(mockedApi.createPOFromUpload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /skip document/i }));

    await screen.findByText('No Purchase Orders Created');
    expect(screen.getByText(/2 skipped/)).toBeInTheDocument();
    expect(screen.getByText('po-1.pdf')).toBeInTheDocument();
    expect(screen.getByText('po-2.pdf')).toBeInTheDocument();
  });

  it('warns when the PO number was already created in this batch and blocks create until it changes', async () => {
    // Both files carry the same extracted PO number; the server-side
    // po_number_exists flag is false for both because neither existed at
    // extraction time.
    mockedApi.uploadPOPdf.mockImplementation(() => Promise.resolve(makeExtraction({ po_number: 'PO-001' })));

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf'), pdfFile('po-2.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    await screen.findByText(/Document 1 of 2/);
    expect(screen.queryByText('This PO number was already created in this batch')).not.toBeInTheDocument();
    fireEvent.click(createPOButton());

    // Doc 2 extracted the number doc 1 just created — the review warns inline.
    await screen.findByText(/Document 2 of 2/);
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-001');
    expect(screen.getByText('This PO number was already created in this batch')).toBeInTheDocument();

    // Create is blocked client-side; the server never sees the duplicate.
    fireEvent.click(createPOButton());
    expect(await screen.findByText(/already created earlier in this batch/i)).toBeInTheDocument();
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(1);

    // Changing the number clears the warning and the create goes through.
    fireEvent.change(screen.getByLabelText(/PO Number/), { target: { value: 'PO-002' } });
    expect(screen.queryByText('This PO number was already created in this batch')).not.toBeInTheDocument();
    fireEvent.click(createPOButton());

    await screen.findByText('2 Purchase Orders Created!');
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(2);
    expect(mockedApi.createPOFromUpload).toHaveBeenLastCalledWith(expect.objectContaining({ po_number: 'PO-002' }));
  });

  it('Upload More Documents on the summary starts a fresh batch in-flow', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction());

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');
    fireEvent.click(createPOButton());
    await screen.findByText('Purchase Order Created!');

    fireEvent.click(screen.getByRole('button', { name: /upload more documents/i }));

    // Back on a clean upload step: no leftovers from the previous batch.
    expect(screen.getByText('Upload Purchasing Document(s)')).toBeInTheDocument();
    expect(screen.queryByText('po-1.pdf')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Browse files')).toBeInTheDocument();
  });
});

describe('POUpload cancellation and stale-response guards', () => {
  it('Retry flips the doc out of error synchronously and cannot double-fire', async () => {
    let failBadPdf = true;
    const retryResolvers: Array<(value: unknown) => void> = [];
    mockedApi.uploadPOPdf.mockImplementation((file: File) => {
      if (file.name === 'bad.pdf') {
        if (failBadPdf) {
          return Promise.reject({ response: { data: { detail: 'Could not parse document' } } });
        }
        return new Promise(resolve => {
          retryResolvers.push(resolve);
        }) as any;
      }
      return Promise.resolve(makeExtraction({ po_number: 'PO-GOOD' }));
    });

    renderPOUpload();
    chooseFiles([pdfFile('bad.pdf'), pdfFile('good.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    await screen.findByText(/Document 2 of 2/);
    fireEvent.click(createPOButton());
    await screen.findByText('Purchase Order Created!');

    failBadPdf = false;
    const retryButton = screen.getByRole('button', { name: 'Retry' });
    fireEvent.click(retryButton);

    // The status flips to 'queued' synchronously: the summary (and its Retry
    // button) is gone before the retry's network call resolves.
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();

    // A second click must not spawn a second pool uploading the same file.
    fireEvent.click(retryButton);
    expect(mockedApi.uploadPOPdf).toHaveBeenCalledTimes(3); // 2 initial + exactly 1 retry
    expect(retryResolvers).toHaveLength(1);

    await act(async () => {
      retryResolvers[0](makeExtraction({ po_number: 'PO-BAD' }));
    });
    await screen.findByText(/Document 1 of 2/);
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-BAD');
  });

  it('Start Over cancels the extraction pool: no new file starts and late completions are dropped', async () => {
    const resolvers: Record<string, (value: unknown) => void> = {};
    mockedApi.uploadPOPdf.mockImplementation(
      (file: File) =>
        new Promise(resolve => {
          resolvers[file.name] = resolve;
        }) as any
    );

    renderPOUpload();
    chooseFiles([
      pdfFile('f1.pdf'),
      pdfFile('f2.pdf', '%PDF-1.4 aa'),
      pdfFile('f3.pdf', '%PDF-1.4 bbb'),
      pdfFile('f4.pdf', '%PDF-1.4 cccc'),
    ]);
    fireEvent.click(extractButton());

    // Concurrency is 2: f1 + f2 in flight, f3/f4 still queued.
    expect(mockedApi.uploadPOPdf).toHaveBeenCalledTimes(2);

    // f1 completes -> its review opens; the freed worker picks up f3.
    await act(async () => {
      resolvers['f1.pdf'](makeExtraction({ po_number: 'PO-F1' }));
    });
    await screen.findByText(/Document 1 of 4/);
    expect(mockedApi.uploadPOPdf).toHaveBeenCalledTimes(3);

    fireEvent.click(screen.getByRole('button', { name: /start over/i }));
    expect(screen.getByText('Upload Purchasing Document(s)')).toBeInTheDocument();

    // The abandoned pool's in-flight requests settle late: they must not
    // mutate the fresh batch's state, and the pool must not start f4.
    await act(async () => {
      resolvers['f2.pdf'](makeExtraction({ po_number: 'PO-F2' }));
      resolvers['f3.pdf'](makeExtraction({ po_number: 'PO-F3' }));
    });
    expect(mockedApi.uploadPOPdf).toHaveBeenCalledTimes(3); // f4 never started
    expect(screen.getByText('Upload Purchasing Document(s)')).toBeInTheDocument();
    expect(screen.queryByText('f2.pdf')).not.toBeInTheDocument();
  });

  it('drops an in-flight part-search response once the review moves to the next document', async () => {
    const unmatchedLine = () =>
      makeLineItem({
        part_match: { matched: false, match_id: null, match_name: '', confidence: 0, suggestions: [] },
        matched_part_id: null,
      });
    mockedApi.uploadPOPdf.mockImplementation((file: File) =>
      Promise.resolve(
        makeExtraction({
          po_number: file.name === 'po-1.pdf' ? 'PO-001' : 'PO-002',
          line_items: [unmatchedLine()],
        })
      )
    );
    let resolveSearch!: (value: unknown) => void;
    mockedApi.searchPartsForPO.mockImplementation(
      () =>
        new Promise(resolve => {
          resolveSearch = resolve;
        }) as any
    );

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf'), pdfFile('po-2.pdf', '%PDF-1.4 longer stub')]);
    fireEvent.click(extractButton());

    await screen.findByText(/Document 1 of 2/);
    fireEvent.change(screen.getByLabelText('Search parts'), { target: { value: 'AN9' } });
    expect(mockedApi.searchPartsForPO).toHaveBeenCalledTimes(1);

    // Advance to doc 2 while doc 1's part search is still in flight.
    fireEvent.click(screen.getByRole('button', { name: /skip document/i }));
    await screen.findByText(/Document 2 of 2/);

    // The stale response must not populate doc 2's same-position line —
    // clicking such a suggestion would assign the wrong part.
    await act(async () => {
      resolveSearch([{ id: 99, part_number: 'STALE-1', name: 'Stale Part' }]);
    });
    expect(screen.queryByText(/STALE-1/)).not.toBeInTheDocument();
  });
});

describe('POUpload review formatting', () => {
  it('renders money with thousands separators and percentages rounded to two places', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(
      makeExtraction({
        vendor_match: {
          matched: true,
          match_id: 5,
          match_name: 'Acme Aerospace',
          confidence: 42.6767676767,
          suggestions: [],
        },
        line_items: [
          makeLineItem({ line_number: 1, qty_ordered: 100, unit_price: 25.92, line_total: 2592 }),
          makeLineItem({
            line_number: 2,
            part_number: 'MS20470AD4',
            qty_ordered: 1,
            unit_price: 8,
            line_total: 8,
            part_match: {
              matched: false,
              match_id: null,
              match_name: '',
              confidence: 0,
              suggestions: [{ id: 7, part_number: 'MS20470AD4-5', name: 'Rivet', score: 90.5 }],
            },
            matched_part_id: null,
          }),
        ],
      })
    );

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');

    // Line total, subtotal, and grand total all run through formatCurrency.
    // 2592 + 8 = 2600 for the totals; the big line renders comma-grouped.
    expect(screen.getByText('$2,592.00')).toBeInTheDocument();
    expect(screen.getByText('100 x $25.92')).toBeInTheDocument();
    expect(screen.getAllByText('$2,600.00')).toHaveLength(2); // Subtotal + Total (no tax/shipping)

    // Vendor-match confidence rounds to two decimals.
    expect(screen.getByText('Confidence: 42.68%')).toBeInTheDocument();

    // Part suggestion scores keep significant decimals, trailing zeros stripped.
    expect(screen.getByRole('button', { name: 'MS20470AD4-5 (90.5%)' })).toBeInTheDocument();
  });

  it('renders no tax row — and no stray "0" text node — when the extracted tax is zero', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction({ tax: 0 }));

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');
    expect(screen.queryByText('Tax:')).not.toBeInTheDocument();
    // The old `tax && (...)` guard leaked a literal "0" text node into the totals.
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });

  it('renders the tax row when the extracted tax is non-zero', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction({ tax: 5 }));

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');
    expect(screen.getByText('Tax:')).toBeInTheDocument();
    expect(screen.getByText('$5.00')).toBeInTheDocument();
  });
});

describe('POUpload single-file happy path', () => {
  it('upload -> review -> create -> success summary, without batch chrome', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction());

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);

    fireEvent.click(screen.getByRole('button', { name: 'Extract Data' }));

    await screen.findByText('Review Extracted Data');
    // Single-document flow shows no batch progress strip.
    expect(screen.queryByText(/Document 1 of 1/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/PO Number/)).toHaveValue('PO-001');
    expect(screen.getByText('Matched: Acme Aerospace')).toBeInTheDocument();

    fireEvent.click(createPOButton());

    await screen.findByText('Purchase Order Created!');
    // Reads like the old single-file success screen: no counts line, no card list.
    expect(screen.getByText('PO PO-001 has been created successfully')).toBeInTheDocument();
    expect(screen.queryByText(/1 created/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to purchasing/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to receiving/i })).toBeInTheDocument();

    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(1);
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledWith(
      expect.objectContaining({
        po_number: 'PO-001',
        vendor_id: 5,
        pdf_path: 'uploads/purchase_orders/stub.pdf',
        line_items: [expect.objectContaining({ part_id: 42, part_number: 'AN960-10L', quantity_ordered: 10 })],
      })
    );
  });

  it('ignores a double-click on Create Purchase Order while a create is in flight', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction());
    let resolveCreate!: (value: { success: boolean; po_id: number; po_number: string }) => void;
    mockedApi.createPOFromUpload.mockImplementation(
      () =>
        new Promise(resolve => {
          resolveCreate = resolve;
        }) as any
    );

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');
    const button = createPOButton();
    fireEvent.click(button);

    // The button disables synchronously, so the second click is a no-op —
    // the backend duplicate check is check-then-insert and must never see
    // two concurrent creates for the same document.
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(1);

    // Skip / Start Over are also locked so the doc under review cannot
    // change while the create is in flight.
    expect(screen.getByRole('button', { name: /skip document/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /start over/i })).toBeDisabled();

    await act(async () => {
      resolveCreate({ success: true, po_id: 101, po_number: 'PO-001' });
    });

    await screen.findByText('Purchase Order Created!');
    expect(mockedApi.createPOFromUpload).toHaveBeenCalledTimes(1);
  });

  it('a create failure surfaces the server detail and stays on review', async () => {
    mockedApi.uploadPOPdf.mockResolvedValue(makeExtraction());
    mockedApi.createPOFromUpload.mockRejectedValue({
      response: { data: { detail: 'PO number already exists' } },
    });

    renderPOUpload();
    chooseFiles([pdfFile('po-1.pdf')]);
    fireEvent.click(extractButton());

    await screen.findByText('Review Extracted Data');
    fireEvent.click(createPOButton());

    await screen.findByText('PO number already exists');
    // Still on review — the doc was not marked created.
    expect(screen.getByText('Review Extracted Data')).toBeInTheDocument();
    expect(screen.queryByText('Purchase Order Created!')).not.toBeInTheDocument();
  });
});
