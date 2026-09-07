import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist';
import PdfPreview from './PdfPreview';

jest.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument: jest.fn() }));
jest.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ __esModule: true, default: 'pdf-worker-url' }), {
  virtual: true,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const mockGetDocument = getDocument as jest.Mock;
let containerWidth = 600;
let resize: (() => void) | undefined;
const disconnect = jest.fn();

function makePdf(pageCount = 2) {
  const renders: Array<{ cancel: jest.Mock; promise: Promise<void> }> = [];
  const renderPage = jest.fn(() => {
    const task = { cancel: jest.fn(), promise: Promise.resolve() };
    renders.push(task);
    return task;
  });
  const page = {
    getViewport: jest.fn(({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale })),
    render: renderPage,
  };
  const pdf = { numPages: pageCount, getPage: jest.fn().mockResolvedValue(page) };
  const destroy = jest.fn().mockResolvedValue(undefined);
  return { pdf, page, renderPage, renders, destroy, task: { promise: Promise.resolve(pdf), destroy } };
}

beforeEach(() => {
  jest.clearAllMocks();
  containerWidth = 600;
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => containerWidth });
  jest.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D);
  Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 2 });
  global.ResizeObserver = class {
    constructor(callback: () => void) {
      resize = callback;
    }
    observe = jest.fn();
    unobserve = jest.fn();
    disconnect = disconnect;
  } as unknown as typeof ResizeObserver;
});

afterEach(() => jest.restoreAllMocks());

async function readyPage(page = 1, total = 2, title = 'Reviewed quote') {
  const canvas = await screen.findByRole('img', { name: `${title} — page ${page} of ${total}` });
  await waitFor(() => expect(canvas).not.toHaveClass('invisible'));
  return canvas as HTMLCanvasElement;
}

it('renders the authorized PDF at the measured width and pages through the complete document', async () => {
  const data = makePdf();
  mockGetDocument.mockReturnValue(data.task);
  render(<PdfPreview url="blob:quote" fileName="Q-123.pdf" title="Reviewed quote" />);
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:quote');
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('download', 'Q-123.pdf');
  expect(screen.getByText('Loading PDF…')).toBeInTheDocument();
  const canvas = await readyPage();
  expect(mockGetDocument).toHaveBeenCalledWith({ url: 'blob:quote' });
  expect(GlobalWorkerOptions.workerSrc).toBe('pdf-worker-url');
  expect(canvas.width).toBe(1200);
  expect(canvas.style.width).toBe('600px');
  expect(screen.getByRole('button', { name: 'Previous PDF page' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Next PDF page' }));
  await readyPage(2);
  expect(data.pdf.getPage).toHaveBeenLastCalledWith(2);
  expect(screen.getByRole('button', { name: 'Next PDF page' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Previous PDF page' }));
  await readyPage();
});

it('keeps the download usable after a document failure and retries the same authorized URL', async () => {
  const failed = {
    promise: Promise.reject(new Error('PDF request failed')),
    destroy: jest.fn().mockResolvedValue(undefined),
  };
  const good = makePdf(1);
  mockGetDocument.mockReturnValueOnce(failed).mockReturnValueOnce(good.task);
  render(<PdfPreview url="blob:quote" title="Reviewed quote" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not display this PDF');
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:quote');
  fireEvent.click(screen.getByRole('button', { name: 'Retry preview' }));
  await readyPage(1, 1);
  expect(mockGetDocument).toHaveBeenCalledTimes(2);
  expect(failed.destroy).toHaveBeenCalledTimes(1);
});

it('reports a canvas rendering failure and can recover with a new render task', async () => {
  const failed = makePdf();
  failed.renderPage.mockImplementation(() => ({
    cancel: jest.fn(),
    promise: Promise.reject(new Error('render failed')),
  }));
  const good = makePdf();
  mockGetDocument.mockReturnValueOnce(failed.task).mockReturnValueOnce(good.task);
  render(<PdfPreview url="blob:quote" title="Reviewed quote" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Retry the preview');
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry preview' }));
  await readyPage();
});

it('cancels an in-flight render on a page change and prevents its late failure from replacing the new page', async () => {
  const data = makePdf();
  const firstRender = deferred<void>();
  const cancel = jest.fn();
  data.renderPage.mockReturnValueOnce({ cancel, promise: firstRender.promise });
  mockGetDocument.mockReturnValue(data.task);
  render(<PdfPreview url="blob:quote" title="Reviewed quote" />);
  await waitFor(() => expect(data.renderPage).toHaveBeenCalledTimes(1));
  const firstCanvas = screen.getByRole('img', { name: 'Reviewed quote — page 1 of 2' });
  fireEvent.click(screen.getByRole('button', { name: 'Next PDF page' }));
  const secondCanvas = await readyPage(2);
  expect(secondCanvas).not.toBe(firstCanvas);
  expect(cancel).toHaveBeenCalledTimes(1);
  await act(async () => firstRender.reject(new Error('RenderingCancelledException')));
  expect(secondCanvas).not.toHaveClass('invisible');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('fits a resized mobile container without overlapping renders and disconnects on unmount', async () => {
  const data = makePdf();
  mockGetDocument.mockReturnValue(data.task);
  const view = render(<PdfPreview url="blob:quote" title="Reviewed quote" />);
  const firstCanvas = await readyPage();
  act(() => {
    containerWidth = 358;
    resize?.();
  });
  const resized = await readyPage();
  expect(resized).not.toBe(firstCanvas);
  expect(resized.style.width).toBe('358px');
  expect(resized.width).toBe(716);
  expect(data.renders[0].cancel).toHaveBeenCalled();
  view.unmount();
  expect(data.destroy).toHaveBeenCalledTimes(1);
  expect(data.renders[1].cancel).toHaveBeenCalledTimes(1);
  expect(disconnect).toHaveBeenCalledTimes(1);
});

it('destroys the old loading task and ignores a late result after switching attachments', async () => {
  const oldDocument = deferred<ReturnType<typeof makePdf>['pdf']>();
  const old = makePdf();
  const current = makePdf(1);
  mockGetDocument
    .mockReturnValueOnce({ promise: oldDocument.promise, destroy: old.destroy })
    .mockReturnValueOnce(current.task);
  const view = render(<PdfPreview url="blob:old" title="Reviewed quote" />);
  await waitFor(() => expect(mockGetDocument).toHaveBeenCalledTimes(1));
  view.rerender(<PdfPreview url="blob:new" title="Reviewed quote" />);
  await readyPage(1, 1);
  await act(async () => oldDocument.resolve(old.pdf));
  expect(old.destroy).toHaveBeenCalledTimes(1);
  expect(old.pdf.getPage).not.toHaveBeenCalled();
  expect(screen.getByText('Page 1 of 1')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:new');
});

it('loads once with StrictMode effect replay and releases the document after closing', async () => {
  const data = makePdf();
  mockGetDocument.mockReturnValue(data.task);
  const view = render(
    <React.StrictMode>
      <PdfPreview url="blob:quote" title="Reviewed quote" />
    </React.StrictMode>
  );
  await readyPage();
  expect(mockGetDocument).toHaveBeenCalledTimes(1);
  view.unmount();
  expect(data.destroy).toHaveBeenCalledTimes(1);
});
