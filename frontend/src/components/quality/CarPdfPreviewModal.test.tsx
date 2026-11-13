/**
 * The printable CAR preview (GET /quality/car/{id}/pdf). What is under test is the blob
 * lifecycle the modal owns, not the pdf.js renderer: the object URL is revoked when the
 * preview closes, a load that lands after the preview closed is revoked rather than kept,
 * and a failed load offers a retry that fetches again.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CarPdfPreviewModal from './CarPdfPreviewModal';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { fetchCARPdf: jest.fn() },
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
  revokeSpy = jest.spyOn(window.URL, 'revokeObjectURL');
});

afterEach(() => {
  revokeSpy.mockRestore();
});

test('shows the saved CAR as <number>.pdf and revokes its object URL when the preview closes', async () => {
  mocked.fetchCARPdf.mockResolvedValue('blob:car-17');
  const onClose = jest.fn();
  const { unmount } = render(<CarPdfPreviewModal carId={17} carNumber="CAR-20261113-002" onClose={onClose} />);

  expect(screen.getByRole('status')).toHaveTextContent(/Loading PDF preview/);
  const preview = await screen.findByTestId('pdf-preview');
  expect(mocked.fetchCARPdf).toHaveBeenCalledWith(17);
  expect(preview).toHaveAttribute('data-url', 'blob:car-17');
  expect(preview).toHaveTextContent('CAR-20261113-002.pdf');
  expect(screen.getByRole('heading', { name: 'Corrective Action Report CAR-20261113-002' })).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(revokeSpy).not.toHaveBeenCalled();

  unmount(); // the parent unmounts the modal on close
  expect(revokeSpy).toHaveBeenCalledWith('blob:car-17');
});

test('a PDF that arrives after the preview closed is revoked, never kept', async () => {
  let resolveLoad: (url: string) => void = () => undefined;
  mocked.fetchCARPdf.mockReturnValue(
    new Promise<string>(resolve => {
      resolveLoad = resolve;
    })
  );
  const { unmount } = render(<CarPdfPreviewModal carId={18} carNumber="CAR-18" onClose={jest.fn()} />);
  await waitFor(() => expect(mocked.fetchCARPdf).toHaveBeenCalledWith(18));

  unmount();
  expect(revokeSpy).not.toHaveBeenCalled();
  await act(async () => {
    resolveLoad('blob:late-car');
  });

  expect(revokeSpy).toHaveBeenCalledWith('blob:late-car');
});

test('a failed load offers a retry that fetches the PDF again', async () => {
  mocked.fetchCARPdf.mockRejectedValueOnce(new Error('503')).mockResolvedValueOnce('blob:car-retry');
  render(<CarPdfPreviewModal carId={19} carNumber="CAR-19" onClose={jest.fn()} />);

  expect(await screen.findByText('Could not load the CAR PDF')).toBeInTheDocument();
  expect(screen.queryByTestId('pdf-preview')).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

  expect(await screen.findByTestId('pdf-preview')).toHaveAttribute('data-url', 'blob:car-retry');
  expect(mocked.fetchCARPdf).toHaveBeenCalledTimes(2);
});
