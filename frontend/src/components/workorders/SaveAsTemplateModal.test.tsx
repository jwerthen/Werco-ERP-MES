/**
 * SaveAsTemplateModal — catalog a work order's plan under a name.
 *
 * Four properties are locked here, each one a decision the obvious
 * implementation gets wrong:
 *
 * 1. **It writes ONE row and touches nothing else.** A template is a name plus a
 *    pointer; the plan is copied at USE time. So this dialog must not navigate,
 *    must not create a work order, and must not write to the source — the test
 *    asserts the only API call made is `createWorkOrderTemplate`.
 *
 * 2. **A laser job has no default quantity to give.** `quantity_ordered` on a
 *    nest-bearing work order is DEFINED as the sum of its nests' planned runs and
 *    is derived at use time, so the field is DISABLED, not hidden, with the
 *    reason on it — and no `default_quantity` is sent.
 *
 * 3. **Server-gated ⇒ non-optimistic.** Names are unique among live templates,
 *    compared CASE-INSENSITIVELY, so a 409 is a refusal this form cannot predict.
 *    It must leave the dialog OPEN with the server's `detail` verbatim, so the
 *    planner can edit the name they already typed — closing over it loses the
 *    typing and the reason at once.
 *
 * 4. **The toast quotes the RESPONSE.** The server collapses whitespace in the
 *    name, so quoting the typed value can name something that is not what got
 *    stored. The fixture makes the two DIFFER so a regression fails.
 *
 * Like every component suite here it mocks `services/api`, so the wire shapes are
 * pinned separately in `services/api.workOrderTemplates.test.ts`. Read both.
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import SaveAsTemplateModal, { SaveAsTemplateSource } from './SaveAsTemplateModal';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { WorkOrderTemplate } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    createWorkOrderTemplate: jest.fn(),
    // Declared so an accidental probe is a visible extra call, not a crash.
    getWorkOrder: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const SOURCE: SaveAsTemplateSource = {
  id: 42,
  work_order_number: 'WO-20260501-004',
  quantity_ordered: 12,
};

const makeTemplate = (overrides: Partial<WorkOrderTemplate> = {}): WorkOrderTemplate => ({
  id: 7,
  name: 'Miratech nest group',
  notes: null,
  source_work_order_id: 42,
  default_quantity: null,
  created_at: '2026-08-25T12:00:00Z',
  updated_at: '2026-08-25T12:00:00Z',
  created_by: 3,
  plan: {
    available: true,
    unavailable_reason: null,
    source_work_order_number: 'WO-20260501-004',
    source_status: 'complete',
    work_order_type: 'production',
    sequential_operations: true,
    priority: 3,
    operation_count: 4,
    nest_count: 0,
    planned_runs_total: 0,
    open_material_tie_count: 0,
    work_centers: ['BRAKE-2'],
    source_quantity_ordered: 12,
  },
  ...overrides,
});

function renderModal({
  open = true,
  workOrder = SOURCE,
  hasLaserNests = false,
}: {
  open?: boolean;
  workOrder?: SaveAsTemplateSource | null;
  hasLaserNests?: boolean;
} = {}) {
  const onClose = jest.fn();
  const onSaved = jest.fn();
  const utils = render(
    <ToastProvider>
      <SaveAsTemplateModal
        open={open}
        workOrder={workOrder}
        hasLaserNests={hasLaserNests}
        onClose={onClose}
        onSaved={onSaved}
      />
    </ToastProvider>
  );
  return { ...utils, onClose, onSaved };
}

const nameInput = () => screen.getByLabelText(/Template name/i) as HTMLInputElement;
const notesInput = () => screen.getByLabelText(/^Notes/i) as HTMLTextAreaElement;
const quantityInput = () => screen.getByLabelText(/Default quantity/i) as HTMLInputElement;
const saveButton = () => screen.getByRole('button', { name: /Sav/i });
const cancelButton = () => screen.getByRole('button', { name: 'Cancel' });

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.createWorkOrderTemplate.mockResolvedValue(makeTemplate());
});

describe('SaveAsTemplateModal: the happy path', () => {
  it('posts the name, note and default quantity against the source work order', async () => {
    renderModal();

    await userEvent.type(nameInput(), 'Miratech nest group');
    await userEvent.type(notesInput(), 'Runs on the Ermaksan.');
    await userEvent.clear(quantityInput());
    await userEvent.type(quantityInput(), '25');
    await userEvent.click(saveButton());

    await waitFor(() =>
      expect(mockApi.createWorkOrderTemplate).toHaveBeenCalledWith({
        source_work_order_id: 42,
        name: 'Miratech nest group',
        notes: 'Runs on the Ermaksan.',
        default_quantity: 25,
      })
    );
  });

  it('omits the optional fields rather than sending empty ones', async () => {
    renderModal({ workOrder: { id: 42, work_order_number: 'WO-20260501-004' } });

    await userEvent.type(nameInput(), '  Bracket brake set  ');
    await userEvent.click(saveButton());

    await waitFor(() => expect(mockApi.createWorkOrderTemplate).toHaveBeenCalled());
    const [payload] = mockApi.createWorkOrderTemplate.mock.calls[0];
    // Trimmed, and the keys the planner left alone are simply absent.
    expect(payload).toEqual({ source_work_order_id: 42, name: 'Bracket brake set' });
  });

  it('reports the saved template and closes, WITHOUT navigating or touching the source', async () => {
    const { onClose, onSaved } = renderModal();

    await userEvent.type(nameInput(), 'Miratech nest group');
    await userEvent.click(saveButton());

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onSaved).toHaveBeenCalledWith(makeTemplate());
    // The ONLY call. A probe of the source work order would be a write in
    // disguise: `getWorkOrder` runs the operation-quantity reconcile and can
    // COMMIT against the very work order this dialog promises not to touch.
    expect(mockApi.getWorkOrder).not.toHaveBeenCalled();
  });

  it('names the template off the RESPONSE, not off the form', async () => {
    // The server collapses whitespace, so the stored name can differ from what
    // was typed. Quoting the form would name something that does not exist.
    mockApi.createWorkOrderTemplate.mockResolvedValue(makeTemplate({ name: 'Miratech nest group' }));
    renderModal();

    await userEvent.type(nameInput(), 'Miratech   nest   group');
    await userEvent.click(saveButton());

    const toast = await screen.findByText(/Saved "Miratech nest group" as a template/);
    expect(toast).toBeInTheDocument();
    expect(screen.queryByText(/Miratech {3}nest {3}group/)).not.toBeInTheDocument();
  });

  it('says the source work order is unchanged — the property planners have to trust', async () => {
    renderModal();

    await userEvent.type(nameInput(), 'Miratech nest group');
    await userEvent.click(saveButton());

    expect(await screen.findByText(/WO-20260501-004 is unchanged/)).toBeInTheDocument();
  });
});

describe('SaveAsTemplateModal: a nest-bearing source has no default quantity to give', () => {
  it('disables the quantity field and says why', async () => {
    renderModal({ hasLaserNests: true });

    expect(quantityInput()).toBeDisabled();
    expect(quantityInput()).toHaveValue(null);
    expect(screen.getByText(/Not used for a laser job/i)).toBeInTheDocument();
    expect(screen.getByText(/sum of its nests’ sheet runs/i)).toBeInTheDocument();
  });

  it('sends no default_quantity at all for a nest-bearing source', async () => {
    renderModal({ hasLaserNests: true });

    await userEvent.type(nameInput(), 'Miratech nest group');
    await userEvent.click(saveButton());

    await waitFor(() => expect(mockApi.createWorkOrderTemplate).toHaveBeenCalled());
    const [payload] = mockApi.createWorkOrderTemplate.mock.calls[0];
    expect('default_quantity' in payload).toBe(false);
  });

  it('leaves the field live and prefilled for an ordinary work order', async () => {
    renderModal({ hasLaserNests: false });

    expect(quantityInput()).toBeEnabled();
    expect(quantityInput()).toHaveValue(12);
    expect(screen.queryByText(/Not used for a laser job/i)).not.toBeInTheDocument();
  });
});

describe('SaveAsTemplateModal: server-gated, therefore non-optimistic', () => {
  it('renders a 409 VERBATIM and keeps the dialog open with the typed name', async () => {
    // Names are unique among live templates, compared case-insensitively, so
    // this refusal is one the form cannot predict. Closing over it would lose
    // both the reason and the typing.
    mockApi.createWorkOrderTemplate.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'A work order template with that name already exists. Pick a different name.' },
      },
    });
    const { onClose, onSaved } = renderModal();

    await userEvent.type(nameInput(), 'Bracket brake set');
    await userEvent.click(saveButton());

    expect(await screen.findByTestId('save-template-error')).toHaveTextContent(
      'A work order template with that name already exists. Pick a different name.'
    );
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(nameInput()).toHaveValue('Bracket brake set');
    expect(onClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('announces the refusal to assistive tech', async () => {
    mockApi.createWorkOrderTemplate.mockRejectedValue({ response: { data: { detail: 'Work order not found' } } });
    renderModal();

    await userEvent.type(nameInput(), 'Gone');
    await userEvent.click(saveButton());

    const alert = await screen.findByTestId('save-template-error');
    expect(alert).toHaveAttribute('role', 'alert');
  });

  it('disables Cancel and refuses Escape while the write is in flight', async () => {
    let resolve!: (template: WorkOrderTemplate) => void;
    mockApi.createWorkOrderTemplate.mockReturnValue(
      new Promise<WorkOrderTemplate>((r) => {
        resolve = r;
      })
    );
    const { onClose } = renderModal();

    await userEvent.type(nameInput(), 'Miratech nest group');
    await userEvent.click(saveButton());

    await waitFor(() => expect(cancelButton()).toBeDisabled());
    // The row may already exist server-side; dismissal must not outrun the answer.
    await userEvent.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();

    resolve(makeTemplate());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('refuses a blank name locally rather than spending a round trip on a 422', async () => {
    renderModal();

    await userEvent.click(saveButton());

    expect(await screen.findByTestId('save-template-error')).toHaveTextContent(
      /Give the template a name/i
    );
    expect(mockApi.createWorkOrderTemplate).not.toHaveBeenCalled();
  });

  it('refuses a non-positive default quantity locally', async () => {
    renderModal();

    await userEvent.type(nameInput(), 'Bracket brake set');
    await userEvent.clear(quantityInput());
    await userEvent.type(quantityInput(), '0');
    await userEvent.click(saveButton());

    expect(await screen.findByTestId('save-template-error')).toHaveTextContent(
      /Default quantity must be greater than zero/i
    );
    expect(mockApi.createWorkOrderTemplate).not.toHaveBeenCalled();
  });
});

describe('SaveAsTemplateModal: what the planner is told will happen', () => {
  it('spells out that nothing is copied now and nothing on the source changes', async () => {
    renderModal();
    const dialog = await screen.findByRole('dialog');

    expect(dialog).toHaveTextContent(/Saves a NAME pointing at/i);
    expect(dialog).toHaveTextContent(/nothing on that work order changes/i);
    expect(dialog).toHaveTextContent(/each use creates a new/i);
    expect(dialog).toHaveTextContent('draft');
  });

  it('caps the name at the column width so an over-long name is not a database error', () => {
    renderModal();
    expect(nameInput()).toHaveAttribute('maxLength', '120');
  });
});
