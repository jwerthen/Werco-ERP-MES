import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { HankSourceFile } from './HankSourceFile';
import type { HankSourcePreview } from '../../types/hankIntake';

beforeEach(() => {
  sessionStorage.clear();
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid: 4, ro: false, type: 'access' }))}.sig`
  );
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: jest.fn(() => 'blob:source') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: jest.fn() });
});
it.each(['docx', 'xlsx', 'xls'] as const)(
  'renders escaped %s text with real source locations and a download, never page links',
  async format => {
    const load = jest.fn().mockResolvedValue(new Blob(['office']));
    const loadPreview = jest
      .fn()
      .mockResolvedValue({
        format,
        units: ['<script>not executable</script>\nPART-1 10 EA'],
        labels: ['Sheet Materials, cells A1:D5'],
        warnings: ['Check formula results.'],
      });
    const { container } = render(
      <HankSourceFile filename={`source.${format}`} load={load} loadPreview={loadPreview} pages={[1, 2]} />
    );
    fireEvent.click(screen.getByRole('button', { name: `Load source: source.${format}` }));
    expect(await screen.findByText(/<script>not executable/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText('Sheet Materials, cells A1:D5')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download source' })).toHaveAttribute('download', `source.${format}`);
    expect(screen.queryByRole('link', { name: /^Page/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /^Open/ })).not.toBeInTheDocument();
    expect(loadPreview).toHaveBeenCalledWith(expect.any(AbortSignal));
  }
);
it('keeps PDF page links and skips the Office preview request', async () => {
  const loadPreview = jest.fn();
  render(
    <HankSourceFile
      filename="source.pdf"
      load={async () => new Blob(['%PDF'], { type: 'application/pdf' })}
      loadPreview={loadPreview}
      pages={[2, 2]}
    />
  );
  fireEvent.click(screen.getByRole('button', { name: 'Load source: source.pdf' }));
  expect(await screen.findByRole('link', { name: 'Page 2' })).toHaveAttribute('href', 'blob:source#page=2');
  expect(screen.getAllByRole('link', { name: 'Page 2' })).toHaveLength(1);
  expect(loadPreview).not.toHaveBeenCalled();
});
it('does not display a late Office preview when the company changes', async () => {
  let resolve!: (value: HankSourcePreview) => void;
  render(
    <HankSourceFile
      filename="source.docx"
      load={async () => new Blob(['office'])}
      loadPreview={() =>
        new Promise(done => {
          resolve = done;
        })
      }
    />
  );
  fireEvent.click(screen.getByRole('button', { name: 'Load source: source.docx' }));
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid: 9, ro: false, type: 'access' }))}.sig`
  );
  await act(async () => {
    resolve({ format: 'docx', units: ['Private source text'], labels: ['Paragraph 1'], warnings: [] });
  });
  expect(screen.queryByText('Private source text')).not.toBeInTheDocument();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});
