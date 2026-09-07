// This entire module is imported only when a preview opens. A static asset
// import inside that lazy boundary lets Vite resolve the worker URL in both
// development (including symlinked dependencies) and production builds.
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = workerUrl;

export { getDocument };
