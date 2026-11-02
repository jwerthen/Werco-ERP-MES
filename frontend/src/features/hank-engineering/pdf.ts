import { extractDrawing, type DrawingPlan } from './model';

/** Local native text only: the PDF bytes never go to an ERP/AI/OCR endpoint. */
export async function readDrawing(file: File): Promise<DrawingPlan> {
  if (!file.name.toLowerCase().endsWith('.pdf') || file.size > 20 * 1024 * 1024)
    throw new Error('Choose a PDF no larger than 20 MB.');
  const bytes = await file.arrayBuffer();
  if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('This file is not a PDF.');
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  const sha = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
  const { getDocument } = await import('../../components/ui/pdfRenderer');
  const task = getDocument({ data: new Uint8Array(bytes) });
  try {
    const pdf = await task.promise;
    if (!pdf.numPages || pdf.numPages > 25) throw new Error('Use a drawing package with 1 to 25 pages.');
    const pages: string[] = [];
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      const lines: { y: number; words: { x: number; width: number; text: string }[] }[] = [];
      for (const item of content.items) {
        if (!('str' in item)) continue;
        const y = item.transform[5];
        let line = lines.find(row => Math.abs(row.y - y) < 2);
        if (!line) {
          line = { y, words: [] };
          lines.push(line);
        }
        line.words.push({ x: item.transform[4], width: item.width, text: item.str });
      }
      pages.push(
        lines
          .sort((a, b) => b.y - a.y)
          .map(line =>
            line.words
              .sort((a, b) => a.x - b.x)
              .map(
                (word, i, words) =>
                  `${i && word.x - words[i - 1].x - words[i - 1].width > 12 ? '\t' : i ? ' ' : ''}${word.text}`
              )
              .join('')
          )
          .join('\n')
      );
      if (pages.join('').length > 200000) throw new Error('Drawing text exceeds the review limit. Split the package.');
    }
    return extractDrawing(file.name, sha, pages);
  } finally {
    await task.destroy();
  }
}
