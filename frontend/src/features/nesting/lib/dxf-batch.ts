import { importDXFWithReport, type Part } from './nesting';

export const MAX_DXF_FILES = 100;
export const MAX_DXF_BYTES = 5_000_000;
export type DXFFile = {
  name: string;
  size: number;
  text: () => Promise<string>;
};
export type ImportResult = {
  name: string;
  status: 'imported' | 'skipped';
  designs: number;
  message: string;
  warnings?: string[];
  footprintOnly?: boolean;
};
export type ImportProgress = { completed: number; total: number; name: string };

/** Read one file at a time so a large batch does not retain every file's text. */
export async function importDXFBatch(
  files: readonly DXFFile[],
  existing: readonly Part[],
  options: {
    units: 'in' | 'mm';
    signal?: AbortSignal;
    onProgress?: (progress: ImportProgress) => void;
    yieldControl?: () => Promise<void>;
  }
) {
  if (files.length > MAX_DXF_FILES) throw new Error('Select up to 100 DXF files at a time. No files were imported.');
  const parts: Part[] = [];
  const results: ImportResult[] = [];
  let quantity = existing.reduce((n, p) => n + p.quantity, 0);
  const vertices = (items: readonly Part[]) =>
    items.reduce(
      (n, p) => n + p.loops.reduce((sum, loop) => sum + (loop.type === 'circle' ? 1 : loop.points.length), 0),
      0
    );
  let points = vertices(existing);
  const yieldControl = options.yieldControl ?? (() => new Promise<void>(resolve => setTimeout(resolve, 0)));
  for (const file of files) {
    options.onProgress?.({
      completed: results.length,
      total: files.length,
      name: file.name,
    });
    await yieldControl();
    try {
      if (options.signal?.aborted) throw new Error('Import cancelled before this file was added.');
      if (existing.length + parts.length >= 300)
        throw new Error('This estimate already contains 300 part designs. Start a new estimate or remove parts.');
      if (quantity >= 300)
        throw new Error('This estimate already contains 300 total parts. Start a new estimate or reduce quantities.');
      if (points >= 20_000)
        throw new Error('This estimate has reached its 20,000-point geometry limit. Split the job into estimates.');
      if (!/\.dxf$/i.test(file.name)) throw new Error('Choose a .dxf file.');
      if (!Number.isFinite(file.size) || file.size < 0 || file.size >= MAX_DXF_BYTES)
        throw new Error('File must be smaller than 5 MB.');
      const text = await file.text();
      if (options.signal?.aborted) throw new Error('Import cancelled before this file was added.');
      const imported = importDXFWithReport(text, file.name, options.units);
      const added = imported.parts.map((part, i) => ({
        ...part,
        color: (existing.length + parts.length + i) % 4,
      }));
      if (existing.length + parts.length + added.length > 300)
        throw new Error('This file would exceed 300 designs in this estimate. Start a new estimate or remove parts.');
      if (quantity + added.length > 300)
        throw new Error(
          'This file would exceed 300 total parts in this estimate. Start a new estimate or reduce quantities.'
        );
      const addedPoints = vertices(added);
      if (points + addedPoints > 20_000)
        throw new Error(
          'This file would exceed the estimate’s 20,000-point geometry limit. Split the job into estimates.'
        );
      parts.push(...added);
      quantity += added.length;
      points += addedPoints;
      results.push({
        name: file.name,
        status: 'imported',
        designs: added.length,
        message: `${added.length} design${added.length === 1 ? '' : 's'} added`,
        warnings: imported.warnings,
        footprintOnly: imported.footprintOnly,
      });
    } catch (error) {
      results.push({
        name: file.name,
        status: 'skipped',
        designs: 0,
        message: error instanceof Error ? error.message : 'Could not read this file.',
      });
    }
    options.onProgress?.({
      completed: results.length,
      total: files.length,
      name: file.name,
    });
  }
  return { parts, results, cancelled: options.signal?.aborted ?? false };
}
