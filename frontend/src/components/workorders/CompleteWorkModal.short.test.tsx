/**
 * CompleteWorkModal — owner ruling 1 (2026-11-05): a short count is reported as scrap
 * with a reason, or the work stays open.
 *
 * The dialog judges every submit with the SERVER's figures (good ceiling, upstream
 * short, the work order's deliverable / accounted loss) so the user reads, before
 * submitting, what the server will decide. These tests pin that it never offers a
 * close the server would refuse, and never blocks one it would accept.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { CompleteWorkModal, CompleteWorkSubmit } from './CompleteWorkModal';

jest.mock('../../hooks/useScrapReasonCodes', () => ({ useScrapReasonCodes: () => ({ codes: [] }) }));

type Props = React.ComponentProps<typeof CompleteWorkModal>;

function renderModal(overrides: Partial<Props> = {}) {
  const onSubmit = jest.fn<Promise<void>, [CompleteWorkSubmit]>().mockResolvedValue(undefined);
  const props: Props = {
    open: true,
    onClose: jest.fn(),
    onSubmit,
    submitting: false,
    title: 'Complete operation "Bend"',
    mode: 'operation',
    target: 10,
    recordedComplete: 6,
    recordedScrapped: 1,
    laborRecorded: true,
    subjectLabel: 'Op 20',
    ...overrides,
  };
  const view = render(<CompleteWorkModal {...props} />);
  const dialog = within(screen.getByRole('dialog'));
  return { ...view, dialog, onSubmit, props };
}

const good = (dialog: ReturnType<typeof within>) => dialog.getByLabelText(/Quantity completed/);
const scrap = (dialog: ReturnType<typeof within>) => dialog.getByLabelText(/Quantity scrapped/);

function chooseLegacyReason(dialog: ReturnType<typeof within>, reason = 'Out of tolerance') {
  fireEvent.click(dialog.getByRole('combobox', { name: /scrap reason/i }));
  fireEvent.mouseDown(screen.getByRole('option', { name: reason }));
}

describe('operation mode', () => {
  it('prefills the RECORDED totals, never the target', () => {
    const { dialog } = renderModal();
    expect(good(dialog)).toHaveValue(6);
    expect(scrap(dialog)).toHaveValue(1);
    expect(good(dialog)).toHaveAccessibleDescription('Total good, including the 6 already recorded.');
  });

  it('reads SHORT, offers only "save and leave open", and disables an unchanged short submit', () => {
    const { dialog } = renderModal();
    const readout = dialog.getByTestId('complete-work-short');
    expect(readout).toHaveTextContent('Short 3 pcs');
    expect(readout).toHaveTextContent('3 of 10 not yet accounted for (6 good, 1 scrapped)');
    expect(readout).toHaveTextContent('Saving keeps Op 20 open');
    expect(readout).toHaveTextContent('Nothing to save yet');
    expect(dialog.getByRole('button', { name: /Save progress · leave open/ })).toBeDisabled();
    expect(dialog.queryByRole('button', { name: /^Complete$/ })).not.toBeInTheDocument();
  });

  it('"Report n as scrap" covers the short; the scrap rise then needs a reason', async () => {
    const { dialog, onSubmit } = renderModal();
    fireEvent.click(dialog.getByRole('button', { name: 'Report 3 as scrap' }));
    expect(scrap(dialog)).toHaveValue(4);
    expect(dialog.queryByTestId('complete-work-short')).not.toBeInTheDocument();
    const submit = dialog.getByRole('button', { name: /^Complete$/ });
    expect(submit).toBeDisabled();
    chooseLegacyReason(dialog);
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith({
      quantityComplete: 6,
      quantityScrapped: 4,
      scrapReason: 'Out of tolerance',
      scrapReasonCodeId: undefined,
      overrideReason: undefined,
    });
  });

  it('asks for a scrap reason only when the total RISES, and never lets it drop', () => {
    const { dialog } = renderModal({ recordedComplete: 9 });
    // Recorded 9 good + 1 scrap = 10: accounted, closable as-is with no new reason.
    expect(dialog.queryByRole('combobox', { name: /scrap reason/i })).not.toBeInTheDocument();
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeEnabled();
    fireEvent.change(scrap(dialog), { target: { value: '0' } });
    expect(dialog.getByText(/Cannot be less than the 1 already recorded/)).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
  });

  it('closes an already-accounted operation with the scrap omitted (the server keeps it)', () => {
    const { dialog, onSubmit } = renderModal({ recordedComplete: 8, recordedScrapped: 2, laborRecorded: false });
    fireEvent.click(dialog.getByRole('button', { name: /^Complete$/ }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ quantityComplete: 8, quantityScrapped: null }));
  });

  it('requires the override reason to book more good than the floor recorded when labor may exist', () => {
    const { dialog, onSubmit } = renderModal({ recordedComplete: 6, recordedScrapped: 0 });
    // The shortcut says up front that booking past the floor count is an assertion.
    fireEvent.click(dialog.getByRole('button', { name: 'Book 10 good (needs a reason)' }));
    expect(good(dialog)).toHaveValue(10);
    const reason = dialog.getByLabelText(/Why record more than the floor counted/);
    expect(reason).toBeRequired();
    expect(reason).toHaveAccessibleDescription(/The floor recorded 6/);
    const submit = dialog.getByRole('button', { name: /^Complete$/ });
    expect(submit).toBeDisabled();
    fireEvent.change(reason, { target: { value: '  Last 4 finished after the operator left  ' } });
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ quantityComplete: 10, overrideReason: 'Last 4 finished after the operator left' })
    );
  });

  it("judges the override against the floor's evidence-floored count, and offers it first", () => {
    // Stored 6, but the floor's labor record shows 8 (an entry the reconcile has not folded
    // in yet): booking 8 needs no reason, and "Use floor count" fills exactly that.
    const { dialog, onSubmit } = renderModal({ recordedComplete: 6, floorRecorded: 8, recordedScrapped: 0 });
    fireEvent.click(dialog.getByRole('button', { name: 'Use floor count (8)' }));
    expect(good(dialog)).toHaveValue(8);
    expect(dialog.queryByLabelText(/Why record more than the floor counted/)).not.toBeInTheDocument();
    // The rest is a short to report, not good to assert.
    expect(dialog.getByRole('button', { name: 'Report 2 as scrap' })).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('keeps desk cleanup of never-clocked work reason-free (the office-exempt asymmetry)', () => {
    const { dialog, onSubmit } = renderModal({ recordedComplete: 0, recordedScrapped: 0, laborRecorded: false });
    fireEvent.click(dialog.getByRole('button', { name: 'Use full count (10)' }));
    const reason = dialog.getByLabelText(/Why record more than the floor counted/);
    expect(reason).not.toBeRequired();
    expect(reason).toHaveAccessibleDescription(/reason is optional/);
    fireEvent.click(dialog.getByRole('button', { name: /^Complete$/ }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ quantityComplete: 10, overrideReason: undefined }));
  });

  it('makes the override reason required when the server asks for it, even on a labor-less row', () => {
    const { dialog } = renderModal({
      recordedComplete: 0,
      recordedScrapped: 0,
      laborRecorded: false,
      error: 'quantity_override_reason is required to record more good than the 0 counted on the floor for Op 20',
    });
    fireEvent.change(good(dialog), { target: { value: '10' } });
    expect(dialog.getByLabelText(/Why record more than the floor counted/)).toBeRequired();
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
  });

  it('caps good at the server ceiling and says why when an upstream close declared a short', () => {
    const { dialog } = renderModal({
      recordedComplete: 6,
      recordedScrapped: 0,
      upstreamShort: 2,
      goodCeiling: 8,
    });
    // 6 of the 8 that reached it: 2 short, and the 2 lost upstream are named.
    const readout = dialog.getByTestId('complete-work-short');
    expect(readout).toHaveTextContent('2 of 8 not yet accounted for');
    expect(readout).toHaveTextContent('(2 never reached it — closed short upstream.)');
    fireEvent.change(good(dialog), { target: { value: '9' } });
    expect(dialog.getByText('Cannot exceed 8 — 2 never reached Op 20 (closed short upstream)')).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: /Complete|Save progress/ })).toBeDisabled();
  });

  it('counts runs on a laser nest', () => {
    const { dialog } = renderModal({ isNest: true, target: 4, recordedComplete: 3, recordedScrapped: 0 });
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent('Short 1 run');
  });

  it('offers "Clock them out and close" for the still-clocked-in refusal, resubmitting with the acknowledgement', () => {
    const { dialog, onSubmit } = renderModal({
      recordedComplete: 8,
      recordedScrapped: 2,
      error:
        'Dana R. is still clocked in to Op 20 — have them clock out with their count first, or leave the operation open. Resubmit with acknowledge_open_entries to clock them out; pieces they have not reported will not be counted.',
    });
    const alert = dialog.getByRole('alert');
    expect(alert).toHaveTextContent('Dana R. is still clocked in to Op 20');
    fireEvent.click(within(alert).getByRole('button', { name: 'Clock them out and close' }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ quantityComplete: 8, quantityScrapped: null, acknowledgeOpenEntries: true })
    );
  });

  it('never offers the acknowledgement for any other refusal', () => {
    const { dialog } = renderModal({ error: 'Operation is on hold and cannot be completed' });
    expect(within(dialog.getByRole('alert')).queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('work-order mode (force-complete)', () => {
  const wo: Partial<Props> = {
    mode: 'work_order',
    title: 'Complete work order WO-0042',
    target: 10,
    recordedComplete: 8,
    recordedScrapped: 0,
    accountedLoss: 2,
    deliverable: 8,
    subjectLabel: 'WO-0042',
  };

  it('accepts a shortfall the floor already reported as scrap', () => {
    const { dialog, onSubmit } = renderModal(wo);
    expect(dialog.queryByTestId('complete-work-short')).not.toBeInTheDocument();
    fireEvent.click(dialog.getByRole('button', { name: /^Complete$/ }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ quantityComplete: 8, quantityScrapped: null }));
  });

  it('refuses a shortfall nothing covers, with the server arithmetic: ordered − good − max(loss, scrap)', () => {
    const { dialog } = renderModal(wo);
    fireEvent.change(good(dialog), { target: { value: '7' } });
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent(
      '1 of 10 unaccounted for (7 complete, 2 scrapped) — report it as scrap with a reason, or leave the work order open.'
    );
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
    fireEvent.click(dialog.getByRole('button', { name: 'Set scrap to 3' }));
    expect(scrap(dialog)).toHaveValue(3);
    expect(dialog.queryByTestId('complete-work-short')).not.toBeInTheDocument();
    // The work-order scrap rose, so a reason is required before it can go.
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
    chooseLegacyReason(dialog);
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeEnabled();
  });

  it('requires the override reason above what the floor recorded (deliverable)', () => {
    const { dialog } = renderModal({ ...wo, accountedLoss: 0 });
    expect(dialog.queryByLabelText(/Why record more than the floor counted/)).not.toBeInTheDocument();
    fireEvent.change(good(dialog), { target: { value: '10' } });
    const reason = dialog.getByLabelText(/Why record more than the floor counted/);
    expect(reason).toBeRequired();
    expect(reason).toHaveAccessibleDescription(/The floor's records support 8/);
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeDisabled();
  });

  it('reads an UNKNOWN floor loss as advisory, never as zero', () => {
    const { dialog } = renderModal({ ...wo, accountedLoss: undefined, deliverable: undefined, recordedScrapped: null });
    fireEvent.change(good(dialog), { target: { value: '7' } });
    expect(dialog.getByTestId('complete-work-short')).toHaveTextContent('3 below the order with no scrap entered here');
    expect(dialog.getByRole('button', { name: /^Complete$/ })).toBeEnabled();
  });
});
