import React, { useEffect, useState } from 'react';
import type { HankSourcePreview } from '../../types/hankIntake';
import { useHankSessionGuard } from './useHankSessionGuard';

export function HankSourceFile({
  filename,
  load,
  loadPreview,
  pages = [],
}: {
  filename: string;
  load: (signal: AbortSignal) => Promise<Blob>;
  loadPreview?: (signal: AbortSignal) => Promise<HankSourcePreview>;
  pages?: number[];
}) {
  const [url, setURL] = useState('');
  const [mime, setMime] = useState('');
  const [preview, setPreview] = useState<HankSourcePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { current, controller, release, changed } = useHankSessionGuard();
  const isOffice = /\.(docx|xlsx|xls)$/i.test(filename);
  const isPDF = /\.pdf$/i.test(filename);
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url]
  );
  useEffect(() => {
    if (changed) {
      setURL('');
      setPreview(null);
    }
  }, [changed]);
  const fetchFile = async () => {
    if (busy || !current()) return;
    const request = controller();
    setBusy(true);
    setError('');
    try {
      const [blob, source] = await Promise.all([
        load(request.signal),
        isOffice && loadPreview ? loadPreview(request.signal) : Promise.resolve(null),
      ]);
      if (current() && !request.signal.aborted) {
        setURL(URL.createObjectURL(blob));
        setMime(blob.type);
        setPreview(source);
      }
    } catch {
      if (current() && !request.signal.aborted) setError('Source file could not be loaded. Try again.');
    } finally {
      release(request);
      if (current()) setBusy(false);
    }
  };
  if (changed) return null;
  return (
    <div className="space-y-2">
      {!url ? (
        <button
          type="button"
          className="text-xs text-fd-blue underline break-all text-left"
          disabled={busy}
          onClick={() => void fetchFile()}
        >
          {busy ? 'Loading source…' : `Load source: ${filename}`}
        </button>
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            {!isOffice && (
              <a href={url} target="_blank" rel="noreferrer" className="text-xs text-fd-blue underline break-all">
                Open {filename}
              </a>
            )}
            <a href={url} download={filename} className="text-xs text-fd-blue underline">
              Download source
            </a>
            {isPDF &&
              Array.from(new Set(pages)).map(page => (
                <a
                  key={page}
                  href={`${url}#page=${page}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-fd-blue underline"
                >
                  Page {page}
                </a>
              ))}
          </div>
          {mime.startsWith('image/') && <img src={url} alt={filename} className="max-h-60 max-w-full object-contain" />}
          {preview && (
            <details open className="space-y-2">
              <summary className="text-xs text-fd-blue cursor-pointer">Source text from {filename}</summary>
              <p className="text-xs text-fd-mute">
                Extracted text preserves source locations. Download the original to verify formatting and formulas.
              </p>
              {preview.warnings.map((warning, index) => (
                <p key={index} className="text-xs text-fd-amber">
                  {warning}
                </p>
              ))}
              <div className="max-h-72 overflow-auto space-y-3 border border-slate-700 p-2">
                {preview.units.map((unit, index) => (
                  <div key={index} className="space-y-1">
                    <p className="text-xs font-semibold text-fd-ink">
                      {preview.labels[index] || `Source section ${index + 1}`}
                    </p>
                    <pre className="whitespace-pre-wrap break-words text-xs text-fd-body font-sans">{unit}</pre>
                  </div>
                ))}
              </div>
            </details>
          )}
        </>
      )}
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
    </div>
  );
}
