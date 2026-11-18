/**
 * Documents — Upload Document: the "Associated Part" is required only for a document that
 * describes one part.
 *
 * A drawing, an inspection plan and an FAI report are found by part number, so the dialog
 * refuses to upload one without its part. Every other type (procedures, work instructions,
 * specifications, forms, quality records, ...) can be company-wide: the part is optional,
 * and "no part" sends NO `part_id` at all. That last point is load-bearing: a revision keeps
 * its predecessor's part link forever, so the form must never push someone into picking a
 * placeholder part just to get past it. `POST /documents/upload` has always accepted a
 * part-less upload; this is a UI rule.
 *
 * The refusal and the help text name each part-bound type in fixed words ("an FAI report"),
 * never the API's type label, so the copy is pinned here verbatim.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Documents, { PART_REQUIRED_DOCUMENT_TYPES } from './Documents';
import api from '../services/api';
import type { Part, UserRole } from '../types';

let mockRole: UserRole = 'quality';
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDocuments: jest.fn(),
    getParts: jest.fn(),
    getDocumentTypes: jest.fn(),
    uploadDocument: jest.fn(),
    deleteDocument: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

interface DocumentTypeChoice {
  value: string;
  label: string;
}

// Every value of the backend `DocumentType` enum, labeled the way
// `GET /documents/types/list` labels it (`value.replace("_", " ").title()`).
const DOCUMENT_TYPE_VALUES = [
  'drawing',
  'specification',
  'work_instruction',
  'inspection_plan',
  'certificate',
  'material_cert',
  'procedure',
  'quality_record',
  'ncr',
  'car',
  'fai',
  'shipping_label',
  'bill_of_lading',
  'receiving_label',
  'other',
];
const DOCUMENT_TYPES: DocumentTypeChoice[] = DOCUMENT_TYPE_VALUES.map(value => ({
  value,
  label: value
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' '),
}));

const OPTIONAL_PART_TYPES = DOCUMENT_TYPE_VALUES.filter(value => !PART_REQUIRED_DOCUMENT_TYPES.has(value));

// How the form names each part-bound type. Written out, never derived from the API's label:
// `GET /documents/types/list` labels `fai` "Fai", which would read "a fai".
const REQUIRED_TYPE_PHRASES: Record<string, { a: string; this: string }> = {
  drawing: { a: 'a drawing', this: 'this drawing' },
  inspection_plan: { a: 'an inspection plan', this: 'this inspection plan' },
  fai: { a: 'an FAI report', this: 'this FAI report' },
};

const OPTIONAL_HELP =
  'Optional. Leave it blank for a company-wide document such as a procedure or form. ' +
  'Every later revision keeps this choice, a part or no part.';

function requiredHelp(documentType: string): string {
  return (
    `Required for ${REQUIRED_TYPE_PHRASES[documentType].a}, so it can be found by part number. ` +
    'Choose the part explicitly; filenames are not used to infer a link.'
  );
}

function refusal(documentType: string): string {
  return `Select the associated part before uploading ${REQUIRED_TYPE_PHRASES[documentType].this} so it can be found by part number.`;
}

const BRACKET: Part = {
  id: 7,
  version: 1,
  part_number: 'PN-700',
  revision: 'A',
  name: 'Bracket',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-11-01T12:00:00Z',
  updated_at: '2026-11-01T12:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
  mockedApi.getParts.mockResolvedValue([BRACKET]);
  mockedApi.getDocumentTypes.mockResolvedValue(DOCUMENT_TYPES);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.uploadDocument.mockResolvedValue({});
});

/** Render the page, open Upload Document, and wait until the type and part choices loaded. */
async function openUploadDialog(): Promise<HTMLElement> {
  render(
    <MemoryRouter>
      <Documents />
    </MemoryRouter>
  );
  // The empty state also offers "Upload Document"; the page header button is the first one.
  fireEvent.click((await screen.findAllByRole('button', { name: 'Upload Document' }))[0]);
  const dialog = screen.getByRole('dialog');
  await within(dialog).findByRole('option', { name: 'PN-700 - Bracket' });
  await within(dialog).findByRole('option', { name: 'Procedure' });
  return dialog;
}

function typeSelect(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('combobox', { name: 'Type' });
}

function partSelect(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('combobox', { name: /Associated Part/ });
}

function fillFileAndTitle(dialog: HTMLElement, fileName: string, title: string) {
  fireEvent.change(within(dialog).getByLabelText(/File/), {
    target: { files: [new File(['%PDF'], fileName, { type: 'application/pdf' })] },
  });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /Title/ }), { target: { value: title } });
}

function submit(dialog: HTMLElement) {
  const form = dialog.querySelector('form');
  if (!form) throw new Error('the upload dialog has no form');
  fireEvent.submit(form);
}

function uploadedFormData(): FormData {
  expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1);
  return mockedApi.uploadDocument.mock.calls[0][0];
}

describe('PART_REQUIRED_DOCUMENT_TYPES', () => {
  it('is exactly the part-bound types: drawing, inspection plan and FAI', () => {
    expect(Array.from(PART_REQUIRED_DOCUMENT_TYPES).sort()).toEqual(['drawing', 'fai', 'inspection_plan']);
  });
});

describe('Upload Document: a company-wide type needs no part', () => {
  it.each(OPTIONAL_PART_TYPES)('uploads a %s with no part and sends no part_id', async documentType => {
    const dialog = await openUploadDialog();
    fireEvent.change(typeSelect(dialog), { target: { value: documentType } });
    fillFileAndTitle(dialog, 'QP-100.pdf', 'QP-100 Sample Procedure');
    expect(partSelect(dialog)).toHaveValue('');

    submit(dialog);

    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    const formData = uploadedFormData();
    expect(formData.has('part_id')).toBe(false);
    expect(formData.get('document_type')).toBe(documentType);
    expect(formData.get('title')).toBe('QP-100 Sample Procedure');
    expect(screen.queryByText(/Select the associated part before uploading/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('offers the part as optional: not required, "No part (company-wide document)", help explains it', async () => {
    const dialog = await openUploadDialog();
    fireEvent.change(typeSelect(dialog), { target: { value: 'procedure' } });

    const part = partSelect(dialog);
    expect(part).not.toBeRequired();
    expect(part).not.toHaveAttribute('aria-required');
    // The label carries no required marker either.
    expect(part).toHaveAccessibleName('Associated Part');
    const noPart = within(part).getByRole('option', { name: 'No part (company-wide document)' });
    expect(noPart).toHaveValue('');
    expect(part).toHaveValue('');
    expect(within(part).queryByRole('option', { name: 'Select the associated part' })).not.toBeInTheDocument();
    expect(part).toHaveAccessibleDescription(OPTIONAL_HELP);
  });

  it('still sends a part that was chosen for an optional type', async () => {
    const dialog = await openUploadDialog();
    fireEvent.change(typeSelect(dialog), { target: { value: 'work_instruction' } });
    fillFileAndTitle(dialog, 'WI-300.pdf', 'WI-300 Bracket Deburr');
    fireEvent.change(partSelect(dialog), { target: { value: '7' } });

    submit(dialog);

    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    const formData = uploadedFormData();
    expect(formData.get('part_id')).toBe('7');
    expect(formData.get('document_type')).toBe('work_instruction');
  });

  it('drops the part again when "No part" is chosen after a part', async () => {
    const dialog = await openUploadDialog();
    fireEvent.change(typeSelect(dialog), { target: { value: 'specification' } });
    fillFileAndTitle(dialog, 'SP-400.pdf', 'SP-400 Paint Specification');
    fireEvent.change(partSelect(dialog), { target: { value: '7' } });
    fireEvent.change(partSelect(dialog), { target: { value: '' } });

    submit(dialog);

    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    expect(uploadedFormData().has('part_id')).toBe(false);
  });
});

describe('Upload Document: a part-bound type still refuses without its part', () => {
  it.each(Array.from(PART_REQUIRED_DOCUMENT_TYPES))(
    'refuses a %s with no part and names the type',
    async documentType => {
      const dialog = await openUploadDialog();
      fireEvent.change(typeSelect(dialog), { target: { value: documentType } });
      fillFileAndTitle(dialog, 'PN-700.pdf', 'PN-700 Bracket');

      const part = partSelect(dialog);
      expect(part).toBeRequired();
      expect(within(part).getByRole('option', { name: 'Select the associated part' })).toHaveValue('');
      expect(within(part).queryByRole('option', { name: 'No part (company-wide document)' })).not.toBeInTheDocument();
      expect(part).toHaveAccessibleDescription(requiredHelp(documentType));

      submit(dialog);

      expect(within(dialog).getByRole('alert').textContent).toBe(refusal(documentType));
      expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog')).toBe(dialog);

      // Choosing the part is all it takes; the part is sent.
      fireEvent.change(part, { target: { value: '7' } });
      submit(dialog);
      await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
      expect(uploadedFormData().get('part_id')).toBe('7');
      expect(uploadedFormData().get('document_type')).toBe(documentType);
    }
  );
});

describe('Upload Document: the type decides whether the part is required', () => {
  it('flips required, the empty option and the help text as the type changes', async () => {
    const dialog = await openUploadDialog();
    const part = partSelect(dialog);

    // The dialog opens on Drawing, a part-bound type.
    expect(typeSelect(dialog)).toHaveValue('drawing');
    expect(part).toBeRequired();
    expect(part).toHaveAccessibleName('Associated Part (required)');
    expect(within(part).getByRole('option', { name: 'Select the associated part' })).toBeInTheDocument();

    fireEvent.change(typeSelect(dialog), { target: { value: 'procedure' } });
    expect(part).not.toBeRequired();
    expect(part).toHaveAccessibleName('Associated Part');
    expect(within(part).getByRole('option', { name: 'No part (company-wide document)' })).toBeInTheDocument();
    expect(part).toHaveAccessibleDescription(OPTIONAL_HELP);

    fireEvent.change(typeSelect(dialog), { target: { value: 'fai' } });
    expect(part).toBeRequired();
    expect(part).toHaveAccessibleName('Associated Part (required)');
    expect(within(part).getByRole('option', { name: 'Select the associated part' })).toBeInTheDocument();
    expect(part).toHaveAccessibleDescription(
      'Required for an FAI report, so it can be found by part number. ' +
        'Choose the part explicitly; filenames are not used to infer a link.'
    );

    fireEvent.change(typeSelect(dialog), { target: { value: 'quality_record' } });
    expect(part).not.toBeRequired();
    expect(part).toHaveAccessibleDescription(OPTIONAL_HELP);
  });

  it('a drawing refused for its missing part uploads part-less once switched to a procedure', async () => {
    const dialog = await openUploadDialog();
    fillFileAndTitle(dialog, 'QP-100.pdf', 'QP-100 Sample Procedure');

    submit(dialog);
    expect(within(dialog).getByRole('alert').textContent).toBe(
      'Select the associated part before uploading this drawing so it can be found by part number.'
    );
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();

    fireEvent.change(typeSelect(dialog), { target: { value: 'procedure' } });
    submit(dialog);

    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    expect(uploadedFormData().has('part_id')).toBe(false);
    expect(uploadedFormData().get('document_type')).toBe('procedure');
  });
});
