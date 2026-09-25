import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { CameraIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { Button } from '../ui/Button';
import { Modal } from '../ui/Modal';

export interface ShopFloorCameraScannerProps {
  open: boolean;
  onClose: () => void;
  /** Resolve the code; reject to leave the scanner open with its manual entry intact. */
  onScan: (code: string) => void | Promise<void>;
}

type CameraState = 'starting' | 'scanning' | 'stopped' | 'error';

function cameraErrorMessage(error: unknown): string {
  const name = error instanceof Error || error instanceof DOMException ? error.name : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Camera access is blocked. Allow camera access in your browser settings, or enter the traveler code below.';
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return 'No camera was found. Enter the traveler code below.';
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return 'The camera is busy. Close other apps using it and try again, or enter the traveler code below.';
  }
  return 'The camera could not start. Try again, or enter the traveler code below.';
}

/** Camera access and decoding stay on the phone; only the decoded text reaches the caller. */
export function ShopFloorCameraScanner({ open, onClose, onScan }: ShopFloorCameraScannerProps) {
  const titleId = useId();
  const inputId = useId();
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cameraRunRef = useRef(0);
  const sessionRef = useRef(0);
  const pendingRef = useRef(false);
  const callbacksRef = useRef({ onClose, onScan });
  const [cameraState, setCameraState] = useState<CameraState>('starting');
  const [cameraAttempt, setCameraAttempt] = useState(0);
  const [error, setError] = useState('');
  const [code, setCode] = useState('');
  const [pending, setPending] = useState(false);

  useEffect(() => {
    callbacksRef.current = { onClose, onScan };
  }, [onClose, onScan]);

  const stopCamera = useCallback(() => {
    // Also invalidate a getUserMedia request whose permission prompt is still open.
    cameraRunRef.current += 1;
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach(track => track.stop());
    streamRef.current = null;
    if (videoRef.current) {
      videoRef.current.pause();
      videoRef.current.srcObject = null;
    }
  }, []);

  const submitCode = useCallback(
    async (value: string) => {
      const trimmed = value.trim();
      if (!trimmed || pendingRef.current) return;
      const session = sessionRef.current;
      pendingRef.current = true;
      setPending(true);
      setCode(trimmed);
      setError('');
      stopCamera();
      setCameraState('stopped');
      try {
        await callbacksRef.current.onScan(trimmed);
        if (session === sessionRef.current) callbacksRef.current.onClose();
      } catch (scanError) {
        if (session === sessionRef.current) {
          setError(
            scanError instanceof Error
              ? scanError.message
              : 'Could not open this traveler. Check the code and try again.'
          );
        }
      } finally {
        if (session === sessionRef.current) {
          pendingRef.current = false;
          setPending(false);
        }
      }
    },
    [stopCamera]
  );

  useEffect(() => {
    sessionRef.current += 1;
    pendingRef.current = false;
    if (open) {
      setCode('');
      setError('');
      setPending(false);
    }
    return () => {
      sessionRef.current += 1;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const video = videoRef.current;
    if (!video) return;
    const run = ++cameraRunRef.current;
    const isCurrent = () => cameraRunRef.current === run;
    setError('');
    setCameraState('starting');

    const start = async () => {
      if (window.isSecureContext === false) {
        setError(
          'Camera scanning needs a secure connection. Open the ERP with https, or enter the traveler code below.'
        );
        setCameraState('error');
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        setError('This browser cannot open the camera. Enter the traveler code below.');
        setCameraState('error');
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
        });
        if (!isCurrent()) {
          stream.getTracks().forEach(track => track.stop());
          return;
        }
        streamRef.current = stream;
        video.srcObject = stream;
        // Lazy JavaScript decoding works on iOS Safari without BarcodeDetector.
        const [{ default: decodeQR }] = await Promise.all([import('qr/decode.js'), video.play()]);
        if (!isCurrent()) return;
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) throw new Error('Camera frames are unavailable');
        setCameraState('scanning');

        const scanFrame = () => {
          if (!isCurrent()) return;
          if (video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0) {
            // Bound work on older phones; the full camera view remains visible.
            const scale = Math.min(1, 960 / Math.max(video.videoWidth, video.videoHeight));
            canvas.width = Math.round(video.videoWidth * scale);
            canvas.height = Math.round(video.videoHeight * scale);
            try {
              context.drawImage(video, 0, 0, canvas.width, canvas.height);
              const value = decodeQR(context.getImageData(0, 0, canvas.width, canvas.height), { timeLimit: 30 });
              if (value.trim()) {
                void submitCode(value);
                return;
              }
            } catch {
              // Most frames contain no readable QR code. Try the next frame.
            }
          }
          timerRef.current = setTimeout(scanFrame, 250);
        };
        scanFrame();
      } catch (cameraError) {
        if (!isCurrent()) return;
        stopCamera();
        setCameraState('error');
        setError(cameraErrorMessage(cameraError));
      }
    };

    const onVisibilityChange = () => {
      if (!document.hidden || !isCurrent()) return;
      stopCamera();
      setCameraState('stopped');
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    void start();
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      stopCamera();
      // The ref may already be cleared by React during unmount.
      video.srcObject = null;
    };
  }, [open, cameraAttempt, stopCamera, submitCode]);

  const close = () => {
    if (pendingRef.current) return;
    stopCamera();
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={close}
      size="md"
      ariaLabelledBy={titleId}
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      <div className="flex items-center justify-between gap-3">
        <h2 id={titleId} className="text-lg font-semibold text-white">
          Scan traveler
        </h2>
        <Button
          variant="ghost"
          className="min-h-11 min-w-11"
          aria-label="Close scanner"
          onClick={close}
          disabled={pending}
        >
          <XMarkIcon className="h-6 w-6" aria-hidden="true" />
        </Button>
      </div>
      <p className="mt-1 text-sm text-slate-300">Point your camera at a traveler’s QR code.</p>
      <div className="relative mt-4 aspect-video overflow-hidden rounded-xl bg-slate-950">
        <video
          ref={videoRef}
          autoPlay
          muted
          playsInline
          className="h-full w-full object-contain"
          aria-label="Traveler camera preview"
        />
        {cameraState !== 'scanning' && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 p-4 text-center text-slate-300">
            <CameraIcon className="h-6 w-6 shrink-0" aria-hidden="true" />
            <span>{cameraState === 'starting' ? 'Starting camera…' : 'Camera off'}</span>
          </div>
        )}
      </div>
      <p role="status" className="mt-2 text-sm text-slate-300">
        {pending
          ? 'Opening traveler…'
          : cameraState === 'scanning'
            ? 'Scanning. Hold the code steady in good light.'
            : cameraState === 'starting'
              ? 'Allow camera access when your browser asks.'
              : 'You can retry the camera or enter the code below.'}
      </p>
      {error && (
        <p
          role="alert"
          className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200"
        >
          {error}
        </p>
      )}
      {(cameraState === 'error' || cameraState === 'stopped') && (
        <Button
          variant="secondary"
          className="mt-3 min-h-11 w-full"
          disabled={pending}
          onClick={() => setCameraAttempt(attempt => attempt + 1)}
        >
          Try camera again
        </Button>
      )}
      <form
        className="mt-5 border-t border-slate-700 pt-4"
        onSubmit={event => {
          event.preventDefault();
          void submitCode(code);
        }}
      >
        <label htmlFor={inputId} className="mb-2 block text-sm font-medium text-slate-200">
          Or enter traveler code
        </label>
        <input
          id={inputId}
          type="text"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={code}
          onChange={event => setCode(event.target.value)}
          onFocus={() => {
            if (cameraState === 'scanning' || cameraState === 'starting') {
              stopCamera();
              setCameraState('stopped');
            }
          }}
          disabled={pending}
          className="input min-h-12 w-full text-base"
          placeholder="Work order number or traveler URL"
        />
        <Button type="submit" className="mt-3 min-h-12 w-full" disabled={pending || !code.trim()}>
          {pending ? 'Opening…' : 'Open traveler'}
        </Button>
      </form>
    </Modal>
  );
}

export default ShopFloorCameraScanner;
