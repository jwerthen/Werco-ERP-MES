import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { UserRole } from '../../types';
import type { HankIntakeBatch, HankIntakeFile, HankIntakePlan } from '../../types/hankIntake';
import type EntityPicker from '../operations/EntityPicker';
import type { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankDocumentIntake } from './HankDocumentIntake';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    getHankIntakes: jest.fn(),
    getHankIntakeFile: jest.fn(),
    createHankIntake: jest.fn(),
    getHankIntakeSource: jest.fn(),
    planHankIntake: jest.fn(),
    commandHankIntake: jest.fn(),
    getDocumentTypes: jest.fn(),
    getPOForReceiving: jest.fn(),
  },
}));
let mockRole: UserRole = 'manager';
jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled, kind }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="">No association</option>
      <option value="7">{kind} 7</option>
      <option value="8">{kind} 8</option>
    </select>
  ),
}));
jest.mock('./HankPurchaseOrderPicker', () => ({
  HankPurchaseOrderPicker: ({
    id,
    value,
    onChange,
    disabled,
  }: React.ComponentProps<typeof HankPurchaseOrderPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="">No purchase order</option>
      <option value="12">PO-0012</option>
      <option value="13">PO-0013</option>
    </select>
  ),
}));

const mockedApi = jest.mocked(api);
const UUID = '11111111-1111-4111-8111-111111111111';
const documentTypes = [
  { value: 'drawing', label: 'Drawing' },
  { value: 'material_cert', label: 'Material certificate' },
  { value: 'other', label: 'Other' },
];
const reviewedFile: HankIntakeFile = {
  id: 51,
  batch_id: 20,
  company_id: 4,
  filename: 'material-cert.pdf',
  file_size: 200,
  content_sha256: 'a'.repeat(64),
  page_count: 2,
  status: 'awaiting_review',
  version: 3,
  source_url: '/api/v1/hank/intake/files/51/source',
  analysis: {
    classification: 'material_certificate',
    confidence: 'low',
    summary: 'A suggested material certificate. Check the source before filing.',
    evidence: [{ page: 2, excerpt: 'MATERIAL CERTIFICATE' }],
    fields: [
      { name: 'heat_number', value: 'H-123?', confidence: 'low', evidence: [{ page: 2, excerpt: 'Heat H-123?' }] },
    ],
    lines: [],
    warnings: ['The heat number needs employee review.'],
    matches: [
      { kind: 'receipt', id: 73, label: 'RCV-0073', href: '/receiving/73', reason: 'Matching supplier and part.' },
    ],
    duplicate_file_ids: [],
    duplicate_document_ids: [],
  },
  plan: null,
  result: null,
  error_message: null,
  created_at: '2026-09-22T13:00:00Z',
  updated_at: '2026-09-22T13:02:00Z',
  completed_at: null,
};
const draftPlan: HankIntakePlan = {
  filing_mode: 'draft',
  title: 'Reviewed material certificate',
  document_type: 'material_cert',
  revision: 'A',
  description: '',
  part_id: null,
  work_order_id: 7,
  vendor_id: null,
  purchase_order_id: null,
  receipt_id: null,
  reviewed_fields: [{ name: 'heat_number', value: 'H-123' }],
  acknowledge_duplicate: false,
};
const plannedFile: HankIntakeFile = {
  ...reviewedFile,
  status: 'planned',
  version: 4,
  plan: {
    input: draftPlan,
    changes: ['File a draft document and link it to WO-0007.'],
    warnings: ['This does not validate certificate contents or approve manufacturing.'],
    references: [],
  },
};
const completedFile: HankIntakeFile = {
  ...plannedFile,
  status: 'completed',
  version: 5,
  completed_at: '2026-09-22T13:05:00Z',
  result: {
    document_id: 91,
    document_number: 'DOC-0091',
    href: '/documents/91',
    summary: 'Filed DOC-0091 as a draft.',
    warnings: ['Document control review is still required.'],
    references: [],
  },
};
const batch: HankIntakeBatch = {
  id: 20,
  company_id: 4,
  request_key: UUID,
  created_at: reviewedFile.created_at,
  files: [reviewedFile],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
function setSession(companyId = 4, readOnly = false) {
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid: companyId, ro: readOnly, type: 'access' }))}.signature`
  );
}
function responseError(status: number, detail: string) {
  return { isAxiosError: true, response: { status, data: { detail } } };
}
function renderIntake(props: Partial<React.ComponentProps<typeof HankDocumentIntake>> = {}) {
  const onNavigate = jest.fn();
  const onBusyChange = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankDocumentIntake onNavigate={onNavigate} onBusyChange={onBusyChange} {...props} />
      </MemoryRouter>
    ),
    onNavigate,
    onBusyChange,
  };
}
function choosePDFs(files = [new File(['%PDF-1.7'], 'source.pdf', { type: 'application/pdf' })]) {
  fireEvent.change(screen.getByLabelText('PDFs to review'), { target: { files } });
  return files;
}
async function openReview(file = reviewedFile) {
  mockedApi.getHankIntakeFile.mockResolvedValue(file);
  const result = renderIntake({ initialId: file.id, workOrderId: 7 });
  await screen.findByRole('form', { name: 'Review document filing' });
  return result;
}
function savePlan() {
  fireEvent.click(screen.getByRole('button', { name: 'Save filing plan for review' }));
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  setSession();
  mockRole = 'manager';
  Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: jest.fn(() => UUID) });
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: jest.fn(() => 'blob:verified-source') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: jest.fn() });
  mockedApi.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: true,
    can_watch: true,
    allowed_kinds: [],
  });
  mockedApi.getHankIntakes.mockResolvedValue({ batches: [], has_more: false, next_before_id: null });
  mockedApi.getHankIntakeFile.mockResolvedValue(reviewedFile);
  mockedApi.createHankIntake.mockResolvedValue(batch);
  mockedApi.getDocumentTypes.mockResolvedValue(documentTypes);
  mockedApi.planHankIntake.mockResolvedValue(plannedFile);
  mockedApi.commandHankIntake.mockResolvedValue(completedFile);
  mockedApi.getHankIntakeSource.mockResolvedValue(new Blob(['%PDF-1.7'], { type: 'application/pdf' }));
  mockedApi.getPOForReceiving.mockImplementation(async id => ({
    lines: [
      {
        receipts: [
          {
            receipt_id: id === 12 ? 73 : 74,
            receipt_number: id === 12 ? 'RCV-0073' : 'RCV-0074',
            quantity_received: 5,
          },
        ],
      },
    ],
  }));
});

describe('Hank document intake', () => {
  it('retries an uncertain upload with the same immutable files and request key', async () => {
    mockedApi.createHankIntake.mockRejectedValueOnce(new Error('Response lost'));
    const { onBusyChange } = renderIntake();
    await screen.findByText('No saved intake batches yet.');
    const files = choosePDFs();
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }));
    await screen.findByText(/Upload was not confirmed/);
    expect(screen.getByLabelText('PDFs to review')).toBeDisabled();
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(mockedApi.createHankIntake).toHaveBeenCalledTimes(1);
    const first = mockedApi.createHankIntake.mock.calls[0][0];
    expect(Array.from(first.entries())).toEqual([
      ['expected_company_id', '4'],
      ['request_key', UUID],
      ['files', files[0]],
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Retry same PDF batch' }));
    await screen.findByRole('button', { name: /material-cert.pdf/ });
    expect(Array.from(mockedApi.createHankIntake.mock.calls[1][0].entries())).toEqual(Array.from(first.entries()));
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
    expect(mockedApi.commandHankIntake).not.toHaveBeenCalled();
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it('unfreezes a confirmed upload refusal so corrected files get a new request key', async () => {
    jest
      .mocked(crypto.randomUUID)
      .mockReturnValueOnce(UUID)
      .mockReturnValueOnce('22222222-2222-4222-8222-222222222222');
    mockedApi.createHankIntake.mockRejectedValueOnce(responseError(422, 'The PDF is encrypted.'));
    renderIntake();
    await screen.findByText('No saved intake batches yet.');
    choosePDFs();
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }));
    await screen.findByText(/The PDF is encrypted/);
    expect(screen.getByLabelText('PDFs to review')).toBeEnabled();
    const replacement = new File(['%PDF-new'], 'corrected.pdf', { type: 'application/pdf' });
    choosePDFs([replacement]);
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }));
    await screen.findByRole('button', { name: /material-cert.pdf/ });
    expect(mockedApi.createHankIntake.mock.calls[1][0].get('request_key')).toBe('22222222-2222-4222-8222-222222222222');
    expect(mockedApi.createHankIntake.mock.calls[1][0].getAll('files')).toEqual([replacement]);
  });

  it.each(['too_many', 'too_large', 'batch_total', 'not_pdf'] as const)(
    'refuses %s files before upload',
    async scenario => {
      renderIntake();
      await screen.findByText('No saved intake batches yet.');
      const count = scenario === 'too_many' ? 6 : scenario === 'batch_total' ? 3 : 1;
      const files = Array.from({ length: count }, (_, id) => {
        const file = new File(['fixture'], scenario === 'not_pdf' ? 'notes.txt' : `${id}.pdf`, {
          type: 'application/pdf',
        });
        if (scenario === 'too_large' || scenario === 'batch_total')
          Object.defineProperty(file, 'size', { value: (scenario === 'too_large' ? 11 : 9) * 1024 * 1024 });
        return file;
      });
      choosePDFs(files);
      expect(screen.getByRole('alert')).toHaveTextContent('Choose up to 5 PDFs');
      expect(screen.getByRole('button', { name: 'Upload for review' })).toBeDisabled();
      expect(mockedApi.createHankIntake).not.toHaveBeenCalled();
    }
  );

  it('saves a corrected draft plan and requires a separate confirmation before displaying the receipt', async () => {
    const saved = deferred<HankIntakeFile>();
    const executed = deferred<HankIntakeFile>();
    mockedApi.planHankIntake.mockReturnValue(saved.promise);
    mockedApi.commandHankIntake.mockReturnValue(executed.promise);
    const { onNavigate } = await openReview();
    expect(mockedApi.getHankIntakeFile).toHaveBeenCalledWith(51, expect.any(AbortSignal));
    expect(mockedApi.getHankIntakes).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Filing action')).toHaveValue('draft');
    expect(screen.getByText('Page 2: Heat H-123?')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'File draft document' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Document title/), {
      target: { value: '  Reviewed material certificate  ' },
    });
    fireEvent.change(screen.getByLabelText(/heat number/), { target: { value: 'H-123' } });
    savePlan();
    await waitFor(() => expect(mockedApi.planHankIntake).toHaveBeenCalledTimes(1));
    expect(mockedApi.planHankIntake).toHaveBeenCalledWith(
      51,
      { expected_company_id: 4, expected_version: 3, plan: draftPlan },
      expect.any(AbortSignal)
    );
    expect(mockedApi.commandHankIntake).not.toHaveBeenCalled();
    expect(screen.queryByText('Filing receipt')).not.toBeInTheDocument();
    await act(async () => saved.resolve(plannedFile));
    expect(screen.getByText(plannedFile.plan!.warnings[0])).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'File draft document' }));
    expect(mockedApi.commandHankIntake).toHaveBeenCalledWith(
      51,
      'execute',
      { expected_company_id: 4, expected_version: 4 },
      expect.any(AbortSignal)
    );
    expect(screen.queryByText(completedFile.result!.summary)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Loading File draft document' })).toBeDisabled();
    await act(async () => executed.resolve(completedFile));
    expect(screen.getByText(completedFile.result!.summary)).toBeInTheDocument();
    expect(screen.getByText(completedFile.result!.warnings[0])).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'File draft document' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'DOC-0091' }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it.each(['analysis', 'saved_plan'] as const)(
    'keeps the %s document type when options finish loading',
    async source => {
      const options = deferred<typeof documentTypes>();
      mockedApi.getDocumentTypes.mockReturnValue(options.promise);
      const file =
        source === 'analysis'
          ? reviewedFile
          : { ...plannedFile, plan: { ...plannedFile.plan!, input: { ...draftPlan, document_type: 'other' } } };
      await openReview(file);
      const expectedType = source === 'analysis' ? 'material_cert' : 'other';
      expect(screen.getByLabelText(/Document type/)).toHaveValue(expectedType);
      await act(async () => options.resolve(documentTypes));
      expect(screen.getByLabelText(/Document type/)).toHaveValue(expectedType);
      savePlan();
      await waitFor(() => expect(mockedApi.planHankIntake).toHaveBeenCalled());
      expect(mockedApi.planHankIntake.mock.calls[0][1].plan.document_type).toBe(expectedType);
    }
  );

  it('requires saving edits before executing an already saved plan', async () => {
    await openReview(plannedFile);
    expect(screen.getByRole('button', { name: 'File draft document' })).toBeEnabled();
    fireEvent.change(screen.getByLabelText(/Document title/), { target: { value: 'A different title' } });
    expect(screen.getByText('Save your changed plan before filing.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'File draft document' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'File draft document' }));
    expect(mockedApi.commandHankIntake).not.toHaveBeenCalled();
  });

  it.each(['private_match', 'legacy_ids'] as const)(
    'offers explicit duplicate acknowledgement for %s',
    async source => {
      const warning = 'An identical file was already submitted through intake; review before filing another copy.';
      await openReview({
        ...reviewedFile,
        analysis: {
          ...reviewedFile.analysis!,
          warnings: [warning],
          ...(source === 'private_match' ? { has_duplicates: true } : {}),
          duplicate_file_ids: source === 'private_match' ? [] : [50],
          duplicate_document_ids: [],
        },
      });
      expect(screen.getByText(warning)).toBeInTheDocument();
      const acknowledgement = screen.getByRole('checkbox', { name: 'I reviewed the possible duplicates' });
      expect(acknowledgement).not.toBeChecked();
      expect(mockedApi.planHankIntake).not.toHaveBeenCalled();
      fireEvent.click(acknowledgement);
      savePlan();
      await waitFor(() => expect(mockedApi.planHankIntake).toHaveBeenCalled());
      expect(mockedApi.planHankIntake.mock.calls[0][1].plan.acknowledge_duplicate).toBe(true);
      expect(mockedApi.commandHankIntake).not.toHaveBeenCalled();
    }
  );

  it('clears a selected receipt when the PO changes and refuses a certificate plan until a new receipt is selected', async () => {
    await openReview();
    fireEvent.change(screen.getByLabelText('Filing action'), { target: { value: 'release_receipt_certificate' } });
    fireEvent.change(screen.getByLabelText('Purchase order reference (optional)'), { target: { value: '12' } });
    await screen.findByRole('option', { name: 'RCV-0073 · received 5' });
    fireEvent.change(screen.getByLabelText('Receipt reference'), { target: { value: '73' } });
    expect(screen.getByLabelText('Receipt reference')).toHaveValue('73');
    fireEvent.change(screen.getByLabelText('Purchase order reference (optional)'), { target: { value: '13' } });
    await screen.findByRole('option', { name: 'RCV-0074 · received 5' });
    expect(screen.getByLabelText('Receipt reference')).toHaveValue('');
    expect(screen.queryByRole('option', { name: 'RCV-0073 · received 5' })).not.toBeInTheDocument();
    savePlan();
    await screen.findByText('Choose the receipt for this certificate.');
    expect(mockedApi.planHankIntake).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Receipt reference'), { target: { value: '74' } });
    savePlan();
    await waitFor(() => expect(mockedApi.planHankIntake).toHaveBeenCalled());
    expect(mockedApi.planHankIntake.mock.calls[0][1].plan).toMatchObject({
      filing_mode: 'release_receipt_certificate',
      purchase_order_id: 13,
      receipt_id: 74,
    });
  });

  it('shows the saved receipt once asynchronously loaded PO receipts become available', async () => {
    const receipts = deferred<Awaited<ReturnType<typeof api.getPOForReceiving>>>();
    mockedApi.getPOForReceiving.mockReturnValue(receipts.promise);
    await openReview({
      ...plannedFile,
      plan: { ...plannedFile.plan!, input: { ...draftPlan, purchase_order_id: 12, receipt_id: 73 } },
    });
    await act(async () =>
      receipts.resolve({
        lines: [{ receipts: [{ receipt_id: 73, receipt_number: 'RCV-0073', quantity_received: 5 }] }],
      })
    );
    expect(screen.getByLabelText('Receipt reference')).toHaveValue('73');
    expect(screen.getByRole('button', { name: 'File draft document' })).toBeEnabled();
  });

  it('requires a refresh after conflict and sends only the refreshed version on the next execution', async () => {
    mockedApi.commandHankIntake.mockRejectedValueOnce(responseError(409, 'Source records changed.'));
    await openReview(plannedFile);
    fireEvent.click(screen.getByRole('button', { name: 'File draft document' }));
    await screen.findByText('Source records changed.');
    expect(screen.getByRole('button', { name: 'File draft document' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save filing plan for review' })).toBeDisabled();
    expect(mockedApi.commandHankIntake).toHaveBeenCalledTimes(1);
    mockedApi.getHankIntakeFile.mockResolvedValueOnce({ ...plannedFile, version: 8 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh file' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'File draft document' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'File draft document' }));
    await screen.findByText(completedFile.result!.summary);
    expect(mockedApi.commandHankIntake).toHaveBeenLastCalledWith(
      51,
      'execute',
      { expected_company_id: 4, expected_version: 8 },
      expect.any(AbortSignal)
    );
  });

  it('aborts a pending filing on company change and ignores the late receipt', async () => {
    const pending = deferred<HankIntakeFile>();
    mockedApi.commandHankIntake.mockReturnValue(pending.promise);
    await openReview(plannedFile);
    fireEvent.click(screen.getByRole('button', { name: 'File draft document' }));
    const signal = mockedApi.commandHankIntake.mock.calls[0][3]!;
    expect(signal.aborted).toBe(false);
    act(() => {
      setSession(5);
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole('alert')).toHaveTextContent('Your session changed. Reopen Hank');
    await act(async () => pending.resolve(completedFile));
    expect(screen.queryByText(completedFile.result!.summary)).not.toBeInTheDocument();
    expect(mockedApi.commandHankIntake).toHaveBeenCalledTimes(1);
  });

  it('loads the authenticated source only on request and cleans up page links on unmount', async () => {
    const { unmount } = await openReview();
    expect(mockedApi.getHankIntakeSource).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Load source: material-cert.pdf' }));
    expect(await screen.findByRole('link', { name: 'Page 2' })).toHaveAttribute('href', 'blob:verified-source#page=2');
    expect(screen.getAllByRole('link', { name: 'Page 2' })).toHaveLength(1);
    expect(mockedApi.getHankIntakeSource).toHaveBeenCalledWith(51, expect.any(AbortSignal));
    expect(screen.getByRole('link', { name: 'Download source' })).toHaveAttribute('download', 'material-cert.pdf');
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:verified-source');
  });

  it.each(['operator', 'read_only'] as const)('keeps %s sessions from uploading or filing', async mode => {
    if (mode === 'operator') mockRole = 'operator';
    else setSession(4, true);
    const list = renderIntake();
    await screen.findByText('No saved intake batches yet.');
    expect(screen.queryByLabelText('PDFs to review')).not.toBeInTheDocument();
    list.unmount();
    await openReview(plannedFile);
    expect(screen.getByRole('button', { name: 'File draft document' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save filing plan for review' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Cancel intake file' })).not.toBeInTheDocument();
    expect(mockedApi.planHankIntake).not.toHaveBeenCalled();
    expect(mockedApi.commandHankIntake).not.toHaveBeenCalled();
  });
});
