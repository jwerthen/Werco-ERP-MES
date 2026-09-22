import React, { useEffect, useState } from 'react';
import { useHankSessionGuard } from './useHankSessionGuard';

export function HankSourceFile({
  filename,
  load,
  pages = [],
}: {
  filename: string;
  load: (signal: AbortSignal) => Promise<Blob>;
  pages?: number[];
}) {
  const [url, setURL] = useState('');
  const [mime, setMime] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { current, controller, release, changed } = useHankSessionGuard();
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url]
  );
  useEffect(() => {
    if (changed) setURL('');
  }, [changed]);
  const fetchFile = async () => {
    if (busy || !current()) return;
    const request = controller();
    setBusy(true);
    setError('');
    try {
      const blob = await load(request.signal);
      if (current() && !request.signal.aborted) {
        setURL(URL.createObjectURL(blob));
        setMime(blob.type);
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
            <a href={url} target="_blank" rel="noreferrer" className="text-xs text-fd-blue underline break-all">
              Open {filename}
            </a>
            <a href={url} download={filename} className="text-xs text-fd-blue underline">
              Download source
            </a>
            {Array.from(new Set(pages)).map(page => (
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
