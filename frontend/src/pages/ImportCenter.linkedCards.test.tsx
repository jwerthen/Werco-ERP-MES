/** The two update/upsert imports of P2 are "linked" cards: their preview/commit wizards live
 *  on the module page, and the card links there and serves the template. */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ImportCenter from './ImportCenter';

jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: 'admin' } }) }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getImportTemplates: jest.fn(), downloadImportTemplate: jest.fn() },
}));
const mocked = api as jest.Mocked<typeof api>;

beforeEach(() => {
  mocked.getImportTemplates.mockResolvedValue({ templates: [] } as any);
});

test.each([
  ['approved_supplier_list', 'Approved Supplier List', '/supplier-scorecards?tab=asl'],
  ['maintenance_schedules', 'PM Schedules', '/maintenance?tab=schedules'],
])('%s links to its module wizard and offers the template', (type, label, href) => {
  render(
    <MemoryRouter initialEntries={[`/import-center?type=${type}`]}>
      <ImportCenter />
    </MemoryRouter>
  );
  expect(screen.getByRole('heading', { name: label })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: `Open ${label}` })).toHaveAttribute('href', href);
  expect(screen.getByRole('button', { name: 'Download template (.xlsx)' })).toBeInTheDocument();
  // Linked imports never mount the direct upload flow here.
  expect(screen.queryByLabelText('Import file')).not.toBeInTheDocument();
});
