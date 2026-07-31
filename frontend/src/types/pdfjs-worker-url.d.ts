/**
 * Vite `?url` asset import for the pdf.js worker (KioskDocViewer's lazy
 * loader). The project doesn't pull in the full `vite/client` type surface, so
 * this declares exactly the one suffixed specifier we use: Vite resolves it to
 * the emitted asset URL string at build time.
 */
declare module 'pdfjs-dist/build/pdf.worker.min.mjs?url' {
  const src: string;
  export default src;
}
