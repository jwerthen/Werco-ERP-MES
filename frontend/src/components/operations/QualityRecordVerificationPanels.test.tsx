/**
 * The two server-stamped verification records on the quality-record detail (migration 127):
 * an NCR's re-verification of reworked or repaired product (POST /quality/ncr/{id}/reverification)
 * and a CAR's effectiveness verification (POST /quality/car/{id}/effectiveness), plus the
 * NCR's read-only "disposition decided" stamp.
 *
 * Rendered through QualityRecordDetail, because the record dialog lives inside the record's
 * own <form onSubmit={save}>: React bubbles the dialog's submit through the component tree
 * even though the Modal is portaled to <body>, so recording must never also save the record.
 * Both verbs are server-gated, so the panels stay non-optimistic and show the server's words.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityRecordDetail from './QualityRecordDetail';
import api from '../../services/api';
import { formatCentralDateTime } from '../../utils/centralTime';
import type { CARRecord, NCRRecord } from '../../types/quality';

let mockCan = true;
jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  default: () => ({ can: () => mockCan }),
}));

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNCR: jest.fn(),
    getCAR: jest.fn(),
    updateNCR: jest.fn(),
    updateCAR: jest.fn(),
    approveCARQuality: jest.fn(),
    fetchCARPdf: jest.fn(),
    recordNcrReverification: jest.fn(),
    recordCarEffectiveness: jest.fn(),
    // The CAR form's part / customer / vendor pickers.
    getParts: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

const DECIDED_AT = '2026-10-30T15:00:00Z';
const VERIFIED_AT = '2026-11-02T20:30:00Z';
const NOTES = 'Re-inspected all five brackets to drawing rev C; edge break within 0.010 max.';
const EVIDENCE = 'Six weeks of final inspection records show no edge burrs.';

const ncr: NCRRecord = {
  id: 7,
  ncr_number: 'NCR-20261105-001',
  title: 'Edge burr on bracket',
  description: 'Burr found on the outside edge at final inspection.',
  status: 'open',
  disposition: 'rework',
  quantity_affected: 5,
  quantity_rejected: 0,
  root_cause: 'Fixture wear let the edge burr through deburr.',
  containment_action: null,
  closed_date: null,
  created_at: '2026-10-29T15:00:00Z',
  updated_at: '2026-11-01T15:00:00Z',
  version: 0,
  action_taken: null,
  concession_reference: null,
  concession_authorized_by: null,
  concession_date: null,
  reverification_result: null,
  reverification_notes: null,
  reverified_by: null,
  reverified_by_name: null,
  reverified_at: null,
  dispositioned_by: 2,
  dispositioned_by_name: 'Dana Quality',
  dispositioned_at: DECIDED_AT,
};

const reverifiedNcr: NCRRecord = {
  ...ncr,
  updated_at: '2026-11-02T20:30:00Z',
  reverification_result: 'conforming',
  reverification_notes: NOTES,
  reverified_by: 3,
  reverified_by_name: 'Robin Inspector',
  reverified_at: VERIFIED_AT,
};

const car: CARRecord = {
  id: 9,
  car_number: 'CAR-20261105-001',
  car_type: 'corrective',
  title: 'Corrective action for edge burr',
  problem_description: 'Repeated edge burrs at final inspection.',
  status: 'verification',
  priority: 3,
  root_cause_analysis: null,
  root_cause: 'Worn deburr fixture.',
  containment_action: null,
  corrective_action: 'Replace and add fixture to PM.',
  preventive_action: null,
  verification_method: 'Inspect 3 consecutive lots.',
  verification_results: 'No burrs in 3 lots.',
  effectiveness_check: 'Draft: first two lots clean.',
  due_date: null,
  verification_due: null,
  closed_date: null,
  created_at: '2026-10-01T15:00:00Z',
  updated_at: '2026-11-01T15:00:00Z',
  human_factors_review: null,
  similar_nonconformities_review: null,
  effectiveness_due: '2026-12-15',
  effectiveness_result: null,
  effectiveness_verified_by: null,
  effectiveness_verified_by_name: null,
  effectiveness_verified_at: null,
};

const verifiedCar: CARRecord = {
  ...car,
  updated_at: '2026-11-02T20:30:00Z',
  effectiveness_check: EVIDENCE,
  effectiveness_result: 'effective',
  effectiveness_verified_by: 3,
  effectiveness_verified_by_name: 'Robin Inspector',
  effectiveness_verified_at: VERIFIED_AT,
};

const renderDetail = (kind: 'ncr' | 'car', handlers = { onClose: jest.fn(), onSaved: jest.fn() }) => {
  render(
    <MemoryRouter>
      <QualityRecordDetail kind={kind} id={kind === 'ncr' ? 7 : 9} {...handlers} />
    </MemoryRouter>
  );
  return handlers;
};

const openDialog = async (buttonName: string, dialogName: string) => {
  fireEvent.click(await screen.findByRole('button', { name: buttonName }));
  return screen.findByRole('dialog', { name: dialogName });
};

beforeEach(() => {
  jest.clearAllMocks();
  mockCan = true;
  mocked.getParts.mockResolvedValue([]);
  mocked.getCustomerNames.mockResolvedValue([]);
  mocked.getVendors.mockResolvedValue([]);
});

// ===================================================================== NCR re-verification

describe('NCR re-verification', () => {
  test.each<[string, Partial<NCRRecord>, boolean]>([
    ['a rework NCR that is open', { disposition: 'rework', status: 'open' }, true],
    ['a repair NCR under review', { disposition: 'repair', status: 'under_review' }, true],
    ['a closed rework NCR', { disposition: 'rework', status: 'closed', closed_date: '2026-11-03' }, false],
    ['a void rework NCR', { disposition: 'rework', status: 'void' }, false],
  ])('%s shows the panel; the record action only where it can be recorded', async (_label, overrides, offered) => {
    mocked.getNCR.mockResolvedValue({ ...ncr, ...overrides });
    renderDetail('ncr');

    const panel = await screen.findByTestId('ncr-reverification');
    const button = within(panel).queryByRole('button', { name: 'Record re-verification' });
    if (offered) expect(button).toBeInTheDocument();
    else expect(button).not.toBeInTheDocument();
  });

  test('a reader without the write role sees the panel but no record action', async () => {
    mockCan = false;
    mocked.getNCR.mockResolvedValue(ncr);
    renderDetail('ncr');

    const panel = await screen.findByTestId('ncr-reverification');
    expect(within(panel).queryByRole('button', { name: 'Record re-verification' })).not.toBeInTheDocument();
  });

  test.each(['pending', 'use_as_is', 'scrap', 'return_to_vendor'] as const)(
    'a %s disposition with nothing recorded shows no re-verification panel',
    async disposition => {
      mocked.getNCR.mockResolvedValue({ ...ncr, disposition });
      renderDetail('ncr');

      await screen.findByTestId('ncr-disposition-stamp');
      expect(screen.queryByTestId('ncr-reverification')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Record re-verification' })).not.toBeInTheDocument();
    }
  );

  test('a stored re-verification stays visible under another disposition, read-only', async () => {
    mocked.getNCR.mockResolvedValue({ ...reverifiedNcr, disposition: 'scrap' });
    renderDetail('ncr');

    const panel = await screen.findByTestId('ncr-reverification');
    expect(panel).toHaveTextContent('Result: Conforming');
    expect(within(panel).queryByRole('button', { name: 'Record re-verification' })).not.toBeInTheDocument();
  });

  test('recording posts the result and trimmed notes, shows the saved record, and does not save the form', async () => {
    const handlers = { onClose: jest.fn(), onSaved: jest.fn() };
    mocked.getNCR.mockResolvedValue(ncr);
    mocked.recordNcrReverification.mockResolvedValue(reverifiedNcr);
    renderDetail('ncr', handlers);

    const dialog = await openDialog('Record re-verification', 'Record re-verification');
    const submit = within(dialog).getByRole('button', { name: 'Record' });
    // A result is a deliberate choice and the notes are required.
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'conforming' } });
    fireEvent.change(within(dialog).getByLabelText(/Re-verification notes/), { target: { value: '   ' } });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Re-verification notes/), { target: { value: `  ${NOTES}  ` } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(mocked.recordNcrReverification).toHaveBeenCalledTimes(1));
    expect(mocked.recordNcrReverification).toHaveBeenCalledWith(7, { result: 'conforming', notes: NOTES });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Record re-verification' })).not.toBeInTheDocument()
    );
    const panel = screen.getByTestId('ncr-reverification');
    expect(panel).toHaveTextContent('Result: Conforming');
    expect(panel).toHaveTextContent(NOTES);
    expect(panel).toHaveTextContent(`Re-verified by Robin Inspector on ${formatCentralDateTime(VERIFIED_AT)}`);
    expect(screen.getByText('Re-verification recorded: conforming.')).toBeInTheDocument();
    expect(handlers.onSaved).toHaveBeenCalled();
    // The dialog's submit bubbles through React to the record's own <form>: recording must
    // not also save (and close) the record.
    expect(mocked.updateNCR).not.toHaveBeenCalled();
    expect(handlers.onClose).not.toHaveBeenCalled();
  });

  test('a refusal stays in the dialog in the server words and changes nothing', async () => {
    const detail = 'NCR NCR-20261105-001 is closed; reopen it to record a re-verification.';
    const handlers = { onClose: jest.fn(), onSaved: jest.fn() };
    mocked.getNCR.mockResolvedValue(ncr);
    mocked.recordNcrReverification.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderDetail('ncr', handlers);

    const dialog = await openDialog('Record re-verification', 'Record re-verification');
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'nonconforming' } });
    fireEvent.change(within(dialog).getByLabelText(/Re-verification notes/), {
      target: { value: 'Two of five still burred.' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
    expect(mocked.recordNcrReverification).toHaveBeenCalledWith(7, {
      result: 'nonconforming',
      notes: 'Two of five still burred.',
    });
    // What was typed is kept for a retry; the record on screen is unchanged.
    expect(within(dialog).getByLabelText(/Re-verification notes/)).toHaveValue('Two of five still burred.');
    expect(screen.getByTestId('ncr-reverification')).toHaveTextContent('Not recorded');
    expect(handlers.onSaved).not.toHaveBeenCalled();
    expect(mocked.updateNCR).not.toHaveBeenCalled();
  });

  test('while the record is in flight the dialog cannot be cancelled or resubmitted', async () => {
    let resolve: (value: NCRRecord) => void = () => {};
    mocked.getNCR.mockResolvedValue(ncr);
    mocked.recordNcrReverification.mockReturnValue(
      new Promise<NCRRecord>(done => {
        resolve = done;
      })
    );
    renderDetail('ncr');

    const dialog = await openDialog('Record re-verification', 'Record re-verification');
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'conforming' } });
    fireEvent.change(within(dialog).getByLabelText(/Re-verification notes/), { target: { value: NOTES } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record' }));

    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled());
    expect(within(dialog).getByRole('button', { name: /Record/ })).toBeDisabled();
    // Nothing is shown as recorded until the server answers.
    expect(screen.getByTestId('ncr-reverification')).toHaveTextContent('Not recorded');

    resolve(reverifiedNcr);
    await waitFor(() => expect(screen.getByTestId('ncr-reverification')).toHaveTextContent('Result: Conforming'));
    expect(mocked.recordNcrReverification).toHaveBeenCalledTimes(1);
  });

  test('the record action waits for unsaved edits to be saved or discarded', async () => {
    mocked.getNCR.mockResolvedValue(ncr);
    renderDetail('ncr');

    const button = await screen.findByRole('button', { name: 'Record re-verification' });
    expect(button).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Containment action'), { target: { value: 'Quarantined lot 7' } });
    expect(button).toBeDisabled();
  });

  test.each<[string, Partial<NCRRecord>, string]>([
    ['a recorded verifier', {}, `Re-verified by Robin Inspector on ${formatCentralDateTime(VERIFIED_AT)}`],
    ['a verifier whose name is not resolved', { reverified_by_name: null }, `Re-verified by user #3 on`],
  ])('the re-verification stamp names %s', async (_label, overrides, text) => {
    mocked.getNCR.mockResolvedValue({ ...reverifiedNcr, ...overrides });
    renderDetail('ncr');

    expect(await screen.findByTestId('ncr-reverification')).toHaveTextContent(text);
  });

  test('an unrecorded re-verification says so', async () => {
    mocked.getNCR.mockResolvedValue(ncr);
    renderDetail('ncr');

    expect(await screen.findByTestId('ncr-reverification')).toHaveTextContent(
      'Not recorded. A conforming re-verification is required to close a rework or repair disposition.'
    );
  });
});

// ================================================================== NCR disposition stamp

describe('NCR disposition stamp', () => {
  test.each<[string, Partial<NCRRecord>, string]>([
    ['decided', {}, `Disposition decided by Dana Quality on ${formatCentralDateTime(DECIDED_AT)}`],
    ['decided by an unresolved user', { dispositioned_by_name: null }, 'Disposition decided by user #2 on'],
    [
      'not decided',
      { disposition: 'pending', dispositioned_by: null, dispositioned_by_name: null, dispositioned_at: null },
      'Disposition decided: Not recorded',
    ],
  ])('reads %s', async (_label, overrides, text) => {
    mocked.getNCR.mockResolvedValue({ ...ncr, ...overrides });
    renderDetail('ncr');

    expect(await screen.findByTestId('ncr-disposition-stamp')).toHaveTextContent(text);
  });

  test('the stamp is never sent back on a save', async () => {
    mocked.getNCR.mockResolvedValue(ncr);
    mocked.updateNCR.mockResolvedValue({ ...ncr, action_taken: 'Hand deburred.' });
    renderDetail('ncr');

    fireEvent.change(await screen.findByLabelText('Action taken'), { target: { value: 'Hand deburred.' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

    await waitFor(() => expect(mocked.updateNCR).toHaveBeenCalledTimes(1));
    const [, payload] = mocked.updateNCR.mock.calls[0];
    expect(payload).toMatchObject({ action_taken: 'Hand deburred.', disposition: 'rework' });
    for (const key of [
      'dispositioned_by',
      'dispositioned_at',
      'reverification_result',
      'reverification_notes',
      'reverified_by',
      'reverified_at',
    ]) {
      expect(payload).not.toHaveProperty(key);
    }
  });
});

// ============================================================ CAR effectiveness verification

describe('CAR effectiveness verification', () => {
  test.each<[string, Partial<CARRecord>, boolean]>([
    ['a CAR in Verification', { status: 'verification' }, true],
    ['a CAR in Corrective Action', { status: 'corrective_action' }, false],
    ['a CAR in Root Cause Analysis', { status: 'root_cause_analysis' }, false],
    ['a closed CAR', { status: 'closed', closed_date: '2026-11-03' }, false],
    ['a void CAR', { status: 'void' }, false],
  ])('%s offers the record action only in Verification', async (_label, overrides, offered) => {
    mocked.getCAR.mockResolvedValue({ ...car, ...overrides });
    renderDetail('car');

    const panel = await screen.findByTestId('car-effectiveness');
    const button = within(panel).queryByRole('button', { name: 'Record effectiveness' });
    if (offered) expect(button).toBeInTheDocument();
    else expect(button).not.toBeInTheDocument();
  });

  test('a reader without the write role is not offered the action', async () => {
    mockCan = false;
    mocked.getCAR.mockResolvedValue(car);
    renderDetail('car');

    const panel = await screen.findByTestId('car-effectiveness');
    expect(within(panel).queryByRole('button', { name: 'Record effectiveness' })).not.toBeInTheDocument();
  });

  test('recording posts the result and trimmed evidence and does not save the form', async () => {
    const handlers = { onClose: jest.fn(), onSaved: jest.fn() };
    mocked.getCAR.mockResolvedValue(car);
    mocked.recordCarEffectiveness.mockResolvedValue(verifiedCar);
    renderDetail('car', handlers);

    const dialog = await openDialog('Record effectiveness', 'Record effectiveness verification');
    // The evidence starts from what the CAR already holds.
    const evidence = within(dialog).getByLabelText(/Evidence reviewed/);
    expect(evidence).toHaveValue('Draft: first two lots clean.');
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'effective' } });
    fireEvent.change(evidence, { target: { value: `  ${EVIDENCE}  ` } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record' }));

    await waitFor(() => expect(mocked.recordCarEffectiveness).toHaveBeenCalledTimes(1));
    expect(mocked.recordCarEffectiveness).toHaveBeenCalledWith(9, { result: 'effective', evidence: EVIDENCE });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Record effectiveness verification' })).not.toBeInTheDocument()
    );
    const panel = screen.getByTestId('car-effectiveness');
    expect(panel).toHaveTextContent('Result: Effective');
    expect(panel).toHaveTextContent(`Evidence reviewed: ${EVIDENCE}`);
    expect(panel).toHaveTextContent(`Verified by Robin Inspector on ${formatCentralDateTime(VERIFIED_AT)}`);
    expect(screen.getByText('Effectiveness verification recorded: effective.')).toBeInTheDocument();
    expect(handlers.onSaved).toHaveBeenCalled();
    // The dialog's submit must not reach the record's own <form onSubmit={save}>.
    expect(mocked.updateCAR).not.toHaveBeenCalled();
    expect(handlers.onClose).not.toHaveBeenCalled();
  });

  test('not effective reports the status the server returned', async () => {
    mocked.getCAR.mockResolvedValue(car);
    mocked.recordCarEffectiveness.mockResolvedValue({
      ...verifiedCar,
      status: 'corrective_action',
      effectiveness_result: 'not_effective',
      effectiveness_check: 'Burrs recurred on lot 44.',
    });
    renderDetail('car');

    const dialog = await openDialog('Record effectiveness', 'Record effectiveness verification');
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'not_effective' } });
    fireEvent.change(within(dialog).getByLabelText(/Evidence reviewed/), {
      target: { value: 'Burrs recurred on lot 44.' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record' }));

    await waitFor(() =>
      expect(mocked.recordCarEffectiveness).toHaveBeenCalledWith(9, {
        result: 'not_effective',
        evidence: 'Burrs recurred on lot 44.',
      })
    );
    expect(
      await screen.findByText(
        'Effectiveness verification recorded: not effective. The CAR is back in Corrective Action.'
      )
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Status')).toHaveValue('corrective_action');
    expect(screen.getByTestId('car-effectiveness')).toHaveTextContent('Result: Not effective');
    expect(screen.queryByRole('button', { name: 'Record effectiveness' })).not.toBeInTheDocument();
    expect(mocked.updateCAR).not.toHaveBeenCalled();
  });

  test('a refusal stays in the dialog in the server words and changes nothing', async () => {
    const detail = 'Record effectiveness once CAR CAR-20261105-001 is in Verification; it is corrective_action.';
    const handlers = { onClose: jest.fn(), onSaved: jest.fn() };
    mocked.getCAR.mockResolvedValue(car);
    mocked.recordCarEffectiveness.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderDetail('car', handlers);

    const dialog = await openDialog('Record effectiveness', 'Record effectiveness verification');
    fireEvent.change(within(dialog).getByLabelText(/Result/), { target: { value: 'effective' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Record' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
    expect(screen.getByTestId('car-effectiveness')).toHaveTextContent('Not recorded.');
    expect(handlers.onSaved).not.toHaveBeenCalled();
    expect(mocked.updateCAR).not.toHaveBeenCalled();
  });

  test('the evidence field is read-only once a result is recorded and is left out of a save', async () => {
    mocked.getCAR.mockResolvedValue(verifiedCar);
    mocked.updateCAR.mockResolvedValue({ ...verifiedCar, preventive_action: 'Add fixture to PM.' });
    renderDetail('car');

    const evidence = await screen.findByLabelText('Effectiveness evidence reviewed');
    expect(evidence).toBeDisabled();
    expect(evidence).toHaveValue(EVIDENCE);
    expect(
      screen.getByText('Recorded with the effectiveness verification. Record a new verification to change it.')
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Preventive action'), { target: { value: 'Add fixture to PM.' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

    await waitFor(() => expect(mocked.updateCAR).toHaveBeenCalledTimes(1));
    const [, payload] = mocked.updateCAR.mock.calls[0];
    expect(payload).toMatchObject({ preventive_action: 'Add fixture to PM.' });
    for (const key of [
      'effectiveness_check',
      'effectiveness_result',
      'effectiveness_verified_by',
      'effectiveness_verified_at',
    ]) {
      expect(payload).not.toHaveProperty(key);
    }
  });

  test('without a recorded result the evidence field stays editable and is saved', async () => {
    mocked.getCAR.mockResolvedValue(car);
    mocked.updateCAR.mockResolvedValue({ ...car, effectiveness_check: 'Three lots clean.' });
    renderDetail('car');

    const evidence = await screen.findByLabelText('Effectiveness evidence reviewed');
    expect(evidence).toBeEnabled();
    fireEvent.change(evidence, { target: { value: 'Three lots clean.' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

    await waitFor(() => expect(mocked.updateCAR).toHaveBeenCalledTimes(1));
    expect(mocked.updateCAR.mock.calls[0][1]).toMatchObject({ effectiveness_check: 'Three lots clean.' });
  });

  test('the record action waits for unsaved edits to be saved or discarded', async () => {
    mocked.getCAR.mockResolvedValue(car);
    renderDetail('car');

    const button = await screen.findByRole('button', { name: 'Record effectiveness' });
    expect(button).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Preventive action'), { target: { value: 'Add fixture to PM.' } });
    expect(button).toBeDisabled();
  });

  test.each<[string, Partial<CARRecord>, string]>([
    ['recorded', {}, `Verified by Robin Inspector on ${formatCentralDateTime(VERIFIED_AT)}`],
    ['recorded by an unresolved user', { effectiveness_verified_by_name: null }, 'Verified by user #3 on'],
  ])('the effectiveness stamp reads %s', async (_label, overrides, text) => {
    mocked.getCAR.mockResolvedValue({ ...verifiedCar, ...overrides });
    renderDetail('car');

    expect(await screen.findByTestId('car-effectiveness')).toHaveTextContent(text);
  });

  test('an unrecorded verification says so, with its due date and what it gates', async () => {
    mocked.getCAR.mockResolvedValue({ ...car, status: 'corrective_action' });
    renderDetail('car');

    const panel = await screen.findByTestId('car-effectiveness');
    expect(panel).toHaveTextContent('Not recorded.');
    expect(panel).toHaveTextContent('An effective verification is required to close a corrective CAR.');
    expect(panel).toHaveTextContent('It is recorded once the CAR is in Verification.');
    expect(panel).not.toHaveTextContent('Verified by');
  });

  test('a preventive CAR is not told an effective result gates its closure', async () => {
    mocked.getCAR.mockResolvedValue({ ...car, car_type: 'preventive' });
    renderDetail('car');

    const panel = await screen.findByTestId('car-effectiveness');
    expect(panel).toHaveTextContent('Not recorded.');
    expect(panel).not.toHaveTextContent('required to close a corrective CAR');
  });
});
