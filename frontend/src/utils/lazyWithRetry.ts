import { lazy, LazyExoticComponent, ComponentType } from 'react';

const CHUNK_RELOAD_KEY = 'werco:chunk-reload-attempted';

function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }

  const message = error.message.toLowerCase();

  return (
    message.includes('chunkloaderror') ||
    message.includes('loading chunk') ||
    message.includes('failed to fetch dynamically imported module') ||
    message.includes("unexpected token '<'")
  );
}

export function lazyWithRetry<T extends ComponentType<unknown>>(
  importer: () => Promise<{ default: T }>
): LazyExoticComponent<T> {
  return lazy(async () => {
    try {
      const module = await importer();

      if (typeof window !== 'undefined') {
        window.sessionStorage.removeItem(CHUNK_RELOAD_KEY);
      }

      return module;
    } catch (error) {
      if (
        typeof window !== 'undefined' &&
        isChunkLoadError(error) &&
        window.sessionStorage.getItem(CHUNK_RELOAD_KEY) !== 'true'
      ) {
        window.sessionStorage.setItem(CHUNK_RELOAD_KEY, 'true');
        window.location.reload();

        return new Promise<never>(() => {});
      }

      if (typeof window !== 'undefined') {
        window.sessionStorage.removeItem(CHUNK_RELOAD_KEY);
      }

      throw error;
    }
  });
}

export default lazyWithRetry;
