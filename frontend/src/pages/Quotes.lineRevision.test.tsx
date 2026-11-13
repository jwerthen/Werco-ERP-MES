jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: 'admin', is_superuser: false } }),
}));
/**
 * Every customer quote line carries the drawing revision it prices (quote_lines.part_revision).
 * The editor enters it (pre-filled from the part's current revision, N/A for a line with no
 * drawing) as `line_revisions` on PUT /quotes/{id}; the detail shows a Rev column; and the
 * server's send gate refuses (409) every door to the customer while a line lacks one — that
 * refusal is shown verbatim.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Quotes from './Quotes';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQuotes: jest.fn(),
    getParts: jest.fn(),
    getQuote: jest.fn(),
    updateQuote: jest.fn(),
    sendQuote: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const quote = {
  id: 7,
  quote_number: 'QUO-0007',
  revision: 'A',
  customer_name: 'Acme',
  status: 'draft',
  quote_date: '2026-11-01',
  valid_until: '2026-12-01',
  updated_at: '2026-11-01T12:00:00',
  subtotal: 140,
  total: 140,
  lines: [
    {
      id: 12,
      line_number: 1,
      part_id: 5,
      part_number: 'PN-5',
      part_revision: null,
      part_current_revision: 'C',
      description: 'Bracket',
      quantity: 4,
      unit_price: 10,
      line_total: 40,
    },
    {
      id: 13,
      line_number: 2,
      part_revision: 'B',
      part_current_revision: null,
      part_number: undefined,
      description: 'Weldment',
      quantity: 1,
      unit_price: 60,
      line_total: 60,
    },
    {
      id: 14,
      line_number: 3,
      part_revision: null,
      part_current_revision: null,
      description: 'Setup charge',
      quantity: 1,
      unit_price: 40,
      line_total: 40,
    },
  ],
};

const renderQuotes = () =>
  render(
    <MemoryRouter initialEntries={['/quotes?id=7']}>
      <ToastProvider>
        <Quotes />
      </ToastProvider>
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQuotes.mockResolvedValue([quote]);
  mockedApi.getParts.mockResolvedValue([]);
});

test('the detail shows each line’s Rev and names the lines still missing one', async () => {
  renderQuotes();
  const table = await screen.findByRole('table', { name: /Quote lines/ });
  expect(within(table).getByRole('columnheader', { name: 'Rev' })).toBeInTheDocument();
  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows[0]).toHaveTextContent('Missing');
  expect(rows[1]).toHaveTextContent('B');
  expect(rows[2]).toHaveTextContent('Missing');
  expect(screen.getByText(/Lines 1, 3 have no drawing revision/)).toBeInTheDocument();
});

test('the editor pre-fills from the part and sends only the revisions that change, upper-cased', async () => {
  mockedApi.updateQuote.mockResolvedValue(quote);
  renderQuotes();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
  const dialog = await screen.findByRole('dialog');

  expect(within(dialog).getByRole('textbox', { name: 'Line 1 drawing revision' })).toHaveValue('C');
  expect(within(dialog).getByRole('textbox', { name: 'Line 2 drawing revision' })).toHaveValue('B');
  expect(within(dialog).getByRole('textbox', { name: 'Line 3 drawing revision' })).toHaveValue('');
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Line 3 drawing revision' }), {
    target: { value: 'n/a' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));

  await waitFor(() => expect(mockedApi.updateQuote).toHaveBeenCalledTimes(1));
  const [, payload] = mockedApi.updateQuote.mock.calls[0];
  // Line 2 already holds B: not re-sent. Line 1's pre-fill is confirmed by the save.
  expect(payload.line_revisions).toEqual([
    { line_id: 12, part_revision: 'C' },
    { line_id: 14, part_revision: 'N/A' },
  ]);
  expect(payload).not.toHaveProperty('lines');
});

test('an invalid or cleared revision is refused before the request', async () => {
  renderQuotes();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit draft' }));
  const dialog = await screen.findByRole('dialog');

  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Line 1 drawing revision' }), {
    target: { value: 'C-1' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Line 1: the drawing revision must be');

  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Line 1 drawing revision' }), {
    target: { value: 'C' },
  });
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Line 2 drawing revision' }), { target: { value: '' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save draft' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'Line 2: a drawing revision, once entered, can be corrected but not cleared.'
  );
  expect(mockedApi.updateQuote).not.toHaveBeenCalled();
});

test('the send gate’s 409 is shown verbatim', async () => {
  const detail = 'Every quote line needs a drawing revision before the quote can be sent. Missing on line(s): 1, 3.';
  mockedApi.sendQuote.mockRejectedValue({ response: { status: 409, data: { detail } } });
  renderQuotes();

  fireEvent.click((await screen.findAllByRole('button', { name: 'Mark as sent' }))[0]);

  expect(await screen.findByText(detail)).toBeInTheDocument();
  expect(mockedApi.sendQuote).toHaveBeenCalledWith(7);
});
