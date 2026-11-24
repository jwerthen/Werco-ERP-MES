/**
 * The issued CoC preview (GET /shipping/{id}/coc/pdf): the blob lifecycle the modal
 * owns -- shown as <coc number>.pdf, revoked on close, a late load revoked rather than
 * kept, and a retry after a failed load.
 */
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import CocPdfPreviewModal from './CocPdfPreviewModal';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { fetchCocPdf: jest.fn() },
}));
jest.mock('../ui/PdfPreview', () => ({
  __esModule: true,
  default: ({ url, fileName }: { url: string; fileName: string }) => (
    <div data-testid="pdf-preview" data-url={url}>
      {fileName}
    </div>
  ),
}));

const mocked = api as jest.Mocked<typeof api>;
let revokeSpy: jest.SpyInstance;
beforeEach(() => {
  jest.clearAllMocks();
  revokeSpy = jest.spyOn(window.URL, 'revokeObjectURL').mockImplementation(() => undefined);
});
afterEach(() => revokeSpy.mockRestore());

test('shows the issued certificate, links it for printing, and revokes it on unmount', async () => {
  mocked.fetchCocPdf.mockResolvedValue('blob:coc-9');
  const { unmount } = render(<CocPdfPreviewModal shipmentId={9} cocNumber="COC-2026-0042" onClose={jest.fn()} />);

  const preview = await screen.findByTestId('pdf-preview');
  expect(mocked.fetchCocPdf).toHaveBeenCalledWith(9);
  expect(preview).toHaveTextContent('COC-2026-0042.pdf');
  expect(screen.getByRole('link', { name: 'Open to print' })).toHaveAttribute('href', 'blob:coc-9');
  unmount();
  expect(revokeSpy).toHaveBeenCalledWith('blob:coc-9');
});

test('a PDF that lands after close is revoked; a failed load retries', async () => {
  let resolveLoad: (url: string) => void = () => undefined;
  mocked.fetchCocPdf.mockReturnValueOnce(new Promise(resolve => (resolveLoad = resolve)));
  const first = render(<CocPdfPreviewModal shipmentId={9} cocNumber="COC-1" onClose={jest.fn()} />);
  first.unmount();
  await act(async () => resolveLoad('blob:late'));
  expect(revokeSpy).toHaveBeenCalledWith('blob:late');

  mocked.fetchCocPdf.mockRejectedValueOnce(new Error('down')).mockResolvedValueOnce('blob:retry');
  render(<CocPdfPreviewModal shipmentId={9} cocNumber="COC-1" onClose={jest.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
  expect(await screen.findByTestId('pdf-preview')).toHaveAttribute('data-url', 'blob:retry');
});
