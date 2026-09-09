import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import BuyerPdfExport from './BuyerPdfExport';
import { NestingPortalContext } from './PortalContext';
import { createBlankProject } from './lib/quote-project';
import { projectFromFile } from './lib/quote-project';
import { remnantStageFixture } from '../../test-utils/remnantStageFixtures';
import { compareSheets, createBlankQuote } from './lib/quoting';
import { rect } from './lib/nesting';
import * as builder from './lib/buyer-pdf';
import type { BuyerPdfInputs, BuyerPdfReport } from './lib/buyer-pdf-types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({ __esModule: true, default: { generateNestingBuyerPdf: jest.fn() } }));
const generate = jest.mocked(api.generateNestingBuyerPdf);
let downloadNames: string[] = [];
function fixture(): BuyerPdfInputs {
  const quote = {
    ...createBlankQuote(),
    name: 'Synthetic job',
    margin: 3.175,
    gap: 3.175,
    options: [
      { id: 'small', width: 101.6, height: 101.6, price: null, enabled: true },
      { id: 'large', width: 152.4, height: 101.6, price: null, enabled: true },
      { id: 'incomplete', width: 12.7, height: 12.7, price: null, enabled: true },
    ],
    parts: [
      { id: 'part1', name: 'Bracket', revision: 'B', quantity: 2, rotate: true, color: 0, loops: [rect(25.4, 25.4)] },
    ],
  };
  const second = {
    ...quote,
    name: 'Second material',
    material: 'Stainless steel',
    thickness: 6.35,
    options: [quote.options[0]],
    parts: quote.parts.map(part => ({ ...part, id: 'part2' })),
  };
  const project = {
    ...createBlankProject(quote),
    name: 'Workspace name',
    groups: [
      { id: 'group.with.dot', quote },
      { id: 'second', quote: second },
    ],
    activeGroupId: 'group.with.dot',
  };
  return {
    project,
    companyId: 2,
    remnantInput: null,
    stages: [],
    comparisons: Object.fromEntries(
      project.groups.map(group => [
        group.id,
        { signature: JSON.stringify(group.quote), comparison: compareSheets(group.quote) },
      ])
    ),
  };
}
function mount(inputs: BuyerPdfInputs, extra: Partial<React.ComponentProps<typeof BuyerPdfExport>> = {}) {
  return render(<BuyerPdfExport inputs={inputs} estimatorId={7} disabled={false} canPlanRemnants {...extra} />, {
    wrapper: ({ children }) => (
      <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider>
    ),
  });
}
async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Export buyer PDF' }));
  await screen.findByRole('dialog', { name: 'Buyer material plan PDF' });
}
beforeEach(() => {
  jest.clearAllMocks();
  downloadNames = [];
  generate.mockResolvedValue(new Blob(['%PDF-1.7\nsynthetic'], { type: 'application/pdf' }));
  jest.spyOn(URL, 'createObjectURL').mockReturnValue('blob:buyer-pdf');
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    downloadNames.push(this.download);
  });
});
afterEach(() => jest.restoreAllMocks());

test('explicit complete choices yield a validated PDF with report-only reference, all quantities and safe filename', async () => {
  const input = fixture(),
    original = JSON.stringify(input);
  mount(input);
  await open();
  const selects = screen.getAllByRole('combobox');
  expect(selects).toHaveLength(2);
  expect(selects[0]).toHaveValue('full:small');
  expect(screen.queryByRole('option', { name: /0\.5/ })).not.toBeInTheDocument();
  fireEvent.change(selects[0], { target: { value: 'full:large' } });
  fireEvent.change(screen.getByLabelText('Job / project reference'), { target: { value: '../Buyer <job>' } });
  fireEvent.change(screen.getByLabelText('Buyer notes (optional)'), { target: { value: 'Confirm A36.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await screen.findByText('Buyer PDF downloaded. No order was submitted.');
  expect(generate).toHaveBeenCalledTimes(1);
  const [report, signal] = generate.mock.calls[0];
  expect(report.expectedCompanyId).toBe(2);
  expect(report.projectName).toBe('../Buyer <job>');
  expect(report.notes).toBe('Confirm A36.');
  expect(report.groups).toHaveLength(2);
  expect(report.groups[0].sheets[0]).toMatchObject({ widthIn: 4, source: 'purchase' });
  expect(report.groups[0].sheets[0].lengthIn).toBeCloseTo(6, 12);
  expect(report.groups.flatMap(group => group.sheets.flatMap(sheet => sheet.placements))).toHaveLength(4);
  expect(signal).toBeInstanceOf(AbortSignal);
  expect(JSON.stringify(input)).toBe(original);
  expect(downloadNames).toEqual(['Buyer job-buyer-material-plan.pdf']);
});

test('empty, stale, missing complete group and permission-denied inputs cannot open export', () => {
  const input = fixture();
  input.comparisons.second.signature = 'old inputs';
  const view = mount(input);
  expect(screen.getByRole('button', { name: 'Export buyer PDF' })).toBeDisabled();
  view.rerender(<BuyerPdfExport inputs={fixture()} estimatorId={7} disabled canPlanRemnants />);
  expect(screen.getByRole('button', { name: 'Export buyer PDF' })).toBeDisabled();
  const empty = { ...fixture(), project: createBlankProject() };
  view.rerender(<BuyerPdfExport inputs={empty} estimatorId={7} disabled={false} canPlanRemnants />);
  expect(screen.getByRole('button', { name: 'Export buyer PDF' })).toBeDisabled();
  expect(generate).not.toHaveBeenCalled();
});

test('double submit is single-flight and closing cancels a pending response without a download', async () => {
  let finish!: (value: Blob) => void;
  generate.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  mount(fixture());
  await open();
  const button = screen.getByRole('button', { name: 'Download buyer PDF' });
  fireEvent.click(button);
  fireEvent.click(button);
  await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Cancel export' }));
  expect(generate.mock.calls[0][1]?.aborted).toBe(true);
  await act(async () => finish(new Blob(['%PDF-1.7'], { type: 'application/pdf' })));
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

test('closing during source validation prevents the request from starting', async () => {
  const input = fixture();
  const report = await builder.buildBuyerPdfReport(
    input,
    { 'group.with.dot': 'full:small', second: 'full:small' },
    { projectName: 'Synthetic', notes: '' }
  );
  let finish!: (value: BuyerPdfReport) => void;
  jest.spyOn(builder, 'buildBuyerPdfReport').mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  mount(input);
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await screen.findByText('Validating selected nests and preparing the PDF…');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel export' }));
  await act(async () => finish(report));
  expect(generate).not.toHaveBeenCalled();
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

test('a validation refusal or corrupt successful response is visible and never downloaded', async () => {
  jest.spyOn(builder, 'buildBuyerPdfReport').mockRejectedValueOnce(new Error('Selected geometry is stale.'));
  mount(fixture());
  await open();
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await screen.findByText('Selected geometry is stale.');
  expect(generate).not.toHaveBeenCalled();
  generate.mockResolvedValueOnce(new Blob(['not PDF'], { type: 'application/pdf' }));
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await screen.findByText('The response is not a PDF. Nothing was downloaded.');
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});

test('recorded-piece selection is explicit, conditional and includes the baseline fallback', async () => {
  const fixture = await remnantStageFixture();
  const input: BuyerPdfInputs = {
    project: projectFromFile(fixture.raw),
    comparisons: {},
    companyId: 2,
    remnantInput: fixture.raw,
    stages: fixture.stages,
  };
  mount(input);
  await open();
  const choices = builder.getBuyerPdfChoices(input)[0];
  expect(screen.getByRole('combobox')).toHaveValue(choices.choices.find(choice => !choice.conditional)!.id);
  expect(screen.queryByText(/Conditional plan:/)).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole('combobox'), {
    target: { value: choices.choices.find(choice => choice.conditional)!.id },
  });
  expect(screen.getByText(/unverified availability and eligibility/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Download buyer PDF' }));
  await screen.findByText('Buyer PDF downloaded. No order was submitted.');
  const group = generate.mock.calls[0][0].groups[0];
  expect(group.selectionKind).toBe('recorded_piece');
  expect(group.baselinePurchaseSheets.length).toBeGreaterThan(0);
  expect(group.sheets.map(sheet => sheet.source)).toEqual(['recorded_piece', 'purchase']);
});
