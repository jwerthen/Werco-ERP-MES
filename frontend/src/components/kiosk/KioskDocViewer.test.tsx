/**
 * KioskDocViewer — the full-screen drawing / nest viewer (Foundry 1h).
 *
 * Everything flows through the INJECTED transport (the StepsTransport pattern):
 * discovery via `fetchOperationDocuments`, bytes via `fetchDocumentBlob` — so
 * both kiosks share one viewer and neither ever drives the /login-redirecting
 * axios client. pdf.js is mocked at the dynamic-import boundary (never pull
 * the real pdfjs-dist into jest) with a rejecting `getDocument`, which forces
 * the component down its `<object>` embed fallback — the fallback IS a
 * supported render path, and it exposes the blob URL as an attribute we can
 * assert on.
 *
 * Locked here:
 *  - tabs derive from discovery (drawing-only → no NEST tab; nest-only →
 *    NEST rendered from initialTab='nest'; both → switchable);
 *  - a blob is fetched PER TAB via the transport, cached by document id;
 *  - transport rejection renders INLINE (verbatim detail, role="alert",
 *    Retry) and never navigates;
 *  - the critical-dims rail renders honest limits from the SPC fields;
 *  - object URLs are revoked on unmount.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import KioskDocViewer, { KioskDocTransport } from './KioskDocViewer';
import type { OperationDocumentsResponse } from '../../types';

// pdf.js stays OUT of jest: the worker ?url specifier cannot resolve here, and
// the library itself is mocked with a rejecting loader so the viewer takes its
// <object> fallback path (a real, supported branch — not a test-only hack).
jest.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: {},
  getDocument: jest.fn(() => ({ promise: Promise.reject(new Error('no pdf.js in jest')) })),
}));
jest.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ __esModule: true, default: 'mock-worker-url' }), {
  virtual: true,
});

const DRAWING_DOC_ID = 11;
const NEST_DOC_ID = 22;

const BASE_DOCS: OperationDocumentsResponse = {
  part: { id: 4, part_number: 'PN-7731', name: 'Bracket, hinge', revision: 'C' },
  drawing: {
    document_id: DRAWING_DOC_ID,
    revision: 'B',
    title: 'Bracket drawing',
    status: 'released',
    released_at: '2026-07-01T12:00:00Z',
    file_name: 'drw.pdf',
  },
  nest: { laser_nest_id: 7, nest_name: 'CNC0042', cnc_number: 'CNC-0042', document_id: NEST_DOC_ID, file_name: 'nest.pdf' },
  material: 'A36',
  critical_dims: [
    { id: 1, name: 'Bore Ø', nominal: 0.5, usl: 0.502, lsl: 0.498, unit_of_measure: 'in' },
    { id: 2, name: 'Min wall', nominal: null, usl: null, lsl: 0.12, unit_of_measure: 'in' },
  ],
};

function makeTransport(docs: OperationDocumentsResponse): {
  transport: KioskDocTransport;
  fetchOperationDocuments: jest.Mock;
  fetchDocumentBlob: jest.Mock;
} {
  const fetchOperationDocuments = jest.fn().mockResolvedValue(docs);
  const fetchDocumentBlob = jest.fn((documentId: number) => Promise.resolve(`blob:doc-${documentId}`));
  return { transport: { fetchOperationDocuments, fetchDocumentBlob }, fetchOperationDocuments, fetchDocumentBlob };
}

/** The <object> embed rendered once the blob lands and pdf.js has bowed out. */
async function findEmbed(): Promise<HTMLElement> {
  return await screen.findByLabelText('Document PDF');
}

describe('KioskDocViewer', () => {
  beforeEach(() => jest.clearAllMocks());

  it('drawing-only discovery renders only the DRAWING tab and fetches its blob', async () => {
    const { transport, fetchOperationDocuments, fetchDocumentBlob } = makeTransport({
      ...BASE_DOCS,
      nest: null,
      material: null,
    });
    render(<KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />);

    expect(await screen.findByTestId('kiosk-viewer-tab-drawing')).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByTestId('kiosk-viewer-tab-nest')).not.toBeInTheDocument();
    expect(fetchOperationDocuments).toHaveBeenCalledWith(31);

    const embed = await findEmbed();
    expect(embed).toHaveAttribute('data', `blob:doc-${DRAWING_DOC_ID}`);
    expect(fetchDocumentBlob).toHaveBeenCalledWith(DRAWING_DOC_ID);
    expect(fetchDocumentBlob).not.toHaveBeenCalledWith(NEST_DOC_ID);
  });

  it('nest-only discovery renders only the NEST tab (opened on it) and fetches the nest blob', async () => {
    const { transport, fetchDocumentBlob } = makeTransport({ ...BASE_DOCS, drawing: null });
    render(<KioskDocViewer operationId={31} initialTab="nest" transport={transport} onBack={jest.fn()} />);

    const nestTab = await screen.findByTestId('kiosk-viewer-tab-nest');
    expect(nestTab).toHaveAttribute('aria-selected', 'true');
    expect(nestTab).toHaveTextContent(/CNC0042/);
    expect(screen.queryByTestId('kiosk-viewer-tab-drawing')).not.toBeInTheDocument();

    expect(await findEmbed()).toHaveAttribute('data', `blob:doc-${NEST_DOC_ID}`);
    expect(fetchDocumentBlob).toHaveBeenCalledWith(NEST_DOC_ID);
  });

  it('switching tabs fetches the OTHER document blob through the transport', async () => {
    const { transport, fetchDocumentBlob } = makeTransport(BASE_DOCS);
    render(<KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />);

    expect(await findEmbed()).toHaveAttribute('data', `blob:doc-${DRAWING_DOC_ID}`);

    fireEvent.click(screen.getByTestId('kiosk-viewer-tab-nest'));
    await waitFor(() => expect(screen.getByLabelText('Document PDF')).toHaveAttribute('data', `blob:doc-${NEST_DOC_ID}`));
    expect(fetchDocumentBlob).toHaveBeenCalledTimes(2);
    expect(fetchDocumentBlob).toHaveBeenLastCalledWith(NEST_DOC_ID);
  });

  it('renders the transport refusal INLINE (verbatim, role=alert, Retry) and never navigates', async () => {
    const pathBefore = window.location.pathname;
    const fetchOperationDocuments = jest
      .fn()
      .mockRejectedValueOnce({ response: { data: { detail: 'Operation not found' } } })
      .mockResolvedValueOnce(BASE_DOCS);
    const transport: KioskDocTransport = {
      fetchOperationDocuments,
      fetchDocumentBlob: jest.fn().mockResolvedValue('blob:doc-11'),
    };
    render(<KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Operation not found');
    // The component itself never redirects — a dead session is the HOST's
    // problem (interceptor guard / badge gate), the viewer only reports.
    expect(window.location.pathname).toBe(pathBefore);

    // Retry re-runs the discovery read in place.
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(await screen.findByTestId('kiosk-viewer-tab-drawing')).toBeInTheDocument();
    expect(fetchOperationDocuments).toHaveBeenCalledTimes(2);
  });

  it('shows the crew sessionExpiredMessage for a 401-shaped transport failure', async () => {
    const err = Object.assign(new Error('Unauthorized'), { status: 401 });
    const transport: KioskDocTransport = {
      fetchOperationDocuments: jest.fn().mockRejectedValue(err),
      fetchDocumentBlob: jest.fn(),
    };
    render(
      <KioskDocViewer
        operationId={31}
        initialTab="drawing"
        transport={transport}
        onBack={jest.fn()}
        sessionExpiredMessage="Badge session expired — rescan to view"
      />
    );

    expect(await screen.findByRole('alert')).toHaveTextContent('Badge session expired — rescan to view');
  });

  it('renders the critical-dims rail with honest limits from the SPC fields', async () => {
    const { transport } = makeTransport(BASE_DOCS);
    render(<KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />);

    expect(await screen.findByText('Critical dims')).toBeInTheDocument();
    expect(screen.getByText('Bore Ø')).toBeInTheDocument();
    expect(screen.getByText('0.498 – 0.502 in')).toBeInTheDocument();
    // LSL-only → a one-sided limit, never an invented range.
    expect(screen.getByText('Min wall')).toBeInTheDocument();
    expect(screen.getByText('≥ 0.12 in')).toBeInTheDocument();
    // Rail key-values per decision 3 (no ECO row, honest MATERIAL from the nest).
    expect(screen.getByText('A36')).toBeInTheDocument();
  });

  it('omits the critical-dims section when the part has none', async () => {
    const { transport } = makeTransport({ ...BASE_DOCS, critical_dims: [] });
    render(<KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />);

    await findEmbed();
    expect(screen.queryByText('Critical dims')).not.toBeInTheDocument();
  });

  it('revokes every fetched blob URL on unmount', async () => {
    const revokeSpy = jest.spyOn(window.URL, 'revokeObjectURL');
    const { transport } = makeTransport(BASE_DOCS);
    const { unmount } = render(
      <KioskDocViewer operationId={31} initialTab="drawing" transport={transport} onBack={jest.fn()} />
    );

    await findEmbed();
    fireEvent.click(screen.getByTestId('kiosk-viewer-tab-nest'));
    await waitFor(() =>
      expect(screen.getByLabelText('Document PDF')).toHaveAttribute('data', `blob:doc-${NEST_DOC_ID}`)
    );

    unmount();
    expect(revokeSpy).toHaveBeenCalledWith(`blob:doc-${DRAWING_DOC_ID}`);
    expect(revokeSpy).toHaveBeenCalledWith(`blob:doc-${NEST_DOC_ID}`);
    revokeSpy.mockRestore();
  });
});
