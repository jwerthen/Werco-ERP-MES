import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import decodeQR from 'qr/decode.js';
import { ShopFloorCameraScanner } from './ShopFloorCameraScanner';

jest.mock('qr/decode.js', () => ({ __esModule: true, default: jest.fn() }));

const decode = decodeQR as jest.Mock;
const getUserMedia = jest.fn();
const stop = jest.fn();
const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream;
const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
const originalSecureContext = Object.getOwnPropertyDescriptor(window, 'isSecureContext');

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderScanner(overrides: Partial<React.ComponentProps<typeof ShopFloorCameraScanner>> = {}) {
  const onClose = jest.fn();
  const onScan = jest.fn().mockResolvedValue(undefined);
  return {
    onClose,
    onScan,
    ...render(<ShopFloorCameraScanner open onClose={onClose} onScan={onScan} {...overrides} />),
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  getUserMedia.mockResolvedValue(stream);
  decode.mockImplementation(() => {
    throw new Error('No QR code');
  });
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
  Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  jest.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  jest.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  jest.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(2);
  jest.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(1280);
  jest.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(720);
  jest.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: jest.fn(),
    getImageData: jest.fn().mockReturnValue({ width: 960, height: 540, data: new Uint8ClampedArray(4) }),
  } as unknown as CanvasRenderingContext2D);
});

afterEach(() => {
  jest.restoreAllMocks();
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices);
  else Reflect.deleteProperty(navigator, 'mediaDevices');
  if (originalSecureContext) Object.defineProperty(window, 'isSecureContext', originalSecureContext);
  else Reflect.deleteProperty(window, 'isSecureContext');
});

describe('ShopFloorCameraScanner', () => {
  it('requests the rear camera without audio and keeps scanning after unreadable frames', async () => {
    const { onScan, unmount } = renderScanner();
    await screen.findByText('Scanning. Hold the code steady in good light.');
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: false,
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
    });
    expect(screen.getByLabelText('Traveler camera preview')).toHaveAttribute('playsinline');
    expect(decode).toHaveBeenCalled();
    expect(onScan).not.toHaveBeenCalled();
    unmount();
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it('decodes without native BarcodeDetector, stops the stream, and submits one result', async () => {
    const resolving = deferred<void>();
    const onScan = jest.fn().mockReturnValue(resolving.promise);
    decode.mockReturnValue(' https://erp.example/work-orders/52?operation=8 ');
    const { onClose } = renderScanner({ onScan });
    await waitFor(() => expect(onScan).toHaveBeenCalledWith('https://erp.example/work-orders/52?operation=8'));
    expect(stop).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Traveler camera preview')).toHaveProperty('srcObject', null);
    expect(screen.getByRole('button', { name: 'Opening…' })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
    expect(onScan).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolving.resolve();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('offers manual entry when access is denied and submits a trimmed code', async () => {
    getUserMedia.mockRejectedValue(new DOMException('Denied', 'NotAllowedError'));
    const { onScan, onClose } = renderScanner();
    expect(await screen.findByRole('alert')).toHaveTextContent('Camera access is blocked');
    const input = screen.getByLabelText('Or enter traveler code');
    fireEvent.change(input, { target: { value: ' WO-0042 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Open traveler' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onScan).toHaveBeenCalledWith('WO-0042');
  });

  it('retains a failed scan for correction and allows the camera to restart', async () => {
    decode.mockReturnValueOnce('WO-missing');
    const onScan = jest.fn().mockRejectedValue(new Error('Traveler not found.'));
    renderScanner({ onScan });
    expect(await screen.findByRole('alert')).toHaveTextContent('Traveler not found.');
    expect(screen.getByLabelText('Or enter traveler code')).toHaveValue('WO-missing');
    fireEvent.click(screen.getByRole('button', { name: 'Try camera again' }));
    await screen.findByText('Scanning. Hold the code steady in good light.');
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Or enter traveler code')).toHaveValue('WO-missing');
  });

  it('stops a permission request that completes after the dialog closes', async () => {
    const permission = deferred<MediaStream>();
    getUserMedia.mockReturnValue(permission.promise);
    const onClose = jest.fn();
    const onScan = jest.fn();
    const { rerender } = render(<ShopFloorCameraScanner open onClose={onClose} onScan={onScan} />);
    fireEvent.click(screen.getByRole('button', { name: 'Close scanner' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(<ShopFloorCameraScanner open={false} onClose={onClose} onScan={onScan} />);
    await act(async () => {
      permission.resolve(stream);
    });
    expect(stop).toHaveBeenCalledTimes(1);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(onScan).not.toHaveBeenCalled();
  });

  it('stops scanning as soon as the dialog closes, even before the parent rerenders', async () => {
    const { onClose } = renderScanner();
    await screen.findByText('Scanning. Hold the code steady in good light.');
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it('releases the camera when the phone switches away from the ERP', async () => {
    renderScanner();
    await screen.findByText('Scanning. Hold the code steady in good light.');
    const hidden = jest.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    fireEvent(document, new Event('visibilitychange'));
    expect(stop).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Camera off')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try camera again' })).toBeEnabled();
    hidden.mockRestore();
  });

  it('stops scanning when an operator chooses manual entry', async () => {
    renderScanner();
    await screen.findByText('Scanning. Hold the code steady in good light.');
    fireEvent.focus(screen.getByLabelText('Or enter traveler code'));
    expect(stop).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Camera off')).toBeInTheDocument();
  });

  it.each([
    ['insecure', false, true, 'secure connection'],
    ['unsupported', true, false, 'cannot open the camera'],
  ])('keeps manual entry usable in an %s browser', async (_name, secure, supported, message) => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: secure });
    if (!supported) Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: undefined });
    renderScanner();
    expect(await screen.findByRole('alert')).toHaveTextContent(String(message));
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Or enter traveler code')).toBeEnabled();
  });

  it('does not request camera access while closed', () => {
    renderScanner({ open: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
