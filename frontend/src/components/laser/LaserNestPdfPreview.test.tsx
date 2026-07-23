import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import LaserNestPdfPreview from './LaserNestPdfPreview';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { fetchLaserNestDocument: jest.fn() },
}));

const mockApi = api as jest.Mocked<typeof api>;

describe('LaserNestPdfPreview', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('fetches the nest PDF and renders it as an <object> with the blob object URL', async () => {
    mockApi.fetchLaserNestDocument.mockResolvedValue('blob:mock-nest-url');

    const { container } = render(<LaserNestPdfPreview laserNestId={9} fileName="nest-9.pdf" />);

    // Loading placeholder first.
    expect(screen.getByTestId('laser-nest-pdf-loading')).toBeInTheDocument();

    await waitFor(() => expect(mockApi.fetchLaserNestDocument).toHaveBeenCalledWith(9));

    // Re-query inside waitFor: the <object> replaces the loading placeholder only
    // after the blob resolves.
    await waitFor(() =>
      expect(container.querySelector('object')).toHaveAttribute('data', 'blob:mock-nest-url')
    );
    const obj = container.querySelector('object');
    expect(obj).toHaveAttribute('type', 'application/pdf');
    expect(obj).toHaveAttribute('aria-label', 'nest-9.pdf');
  });

  it('revokes the object URL on unmount', async () => {
    mockApi.fetchLaserNestDocument.mockResolvedValue('blob:revoke-me');
    const revokeSpy = jest.spyOn(window.URL, 'revokeObjectURL');

    const { unmount } = render(<LaserNestPdfPreview laserNestId={1} />);
    await waitFor(() => expect(mockApi.fetchLaserNestDocument).toHaveBeenCalled());

    unmount();
    expect(revokeSpy).toHaveBeenCalledWith('blob:revoke-me');
    revokeSpy.mockRestore();
  });

  it('shows an error state when the fetch fails', async () => {
    mockApi.fetchLaserNestDocument.mockRejectedValue(new Error('403'));
    render(<LaserNestPdfPreview laserNestId={2} />);
    expect(await screen.findByText(/could not load the nest pdf/i)).toBeInTheDocument();
  });

  // Kiosk transport injection (Kiosk Foundry Redesign): a provided fetchBlob
  // fully replaces the global-axios default so fenced kiosk surfaces never
  // drive the /login-redirecting client.
  it('uses an injected fetchBlob instead of the api client when provided', async () => {
    const fetchBlob = jest.fn().mockResolvedValue('blob:kiosk-transport');

    const { container } = render(
      <LaserNestPdfPreview laserNestId={7} fileName="nest-7.pdf" fetchBlob={fetchBlob} />
    );

    await waitFor(() =>
      expect(container.querySelector('object')).toHaveAttribute('data', 'blob:kiosk-transport')
    );
    expect(fetchBlob).toHaveBeenCalledTimes(1);
    expect(mockApi.fetchLaserNestDocument).not.toHaveBeenCalled();
  });

  it('shows the error state when the injected fetchBlob rejects (never navigates)', async () => {
    const fetchBlob = jest.fn().mockRejectedValue(new Error('badge session expired'));
    render(<LaserNestPdfPreview laserNestId={8} fetchBlob={fetchBlob} />);
    expect(await screen.findByText(/could not load the nest pdf/i)).toBeInTheDocument();
    expect(mockApi.fetchLaserNestDocument).not.toHaveBeenCalled();
  });
});
