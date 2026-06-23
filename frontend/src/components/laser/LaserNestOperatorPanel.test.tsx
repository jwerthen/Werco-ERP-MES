import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LaserNestOperatorPanel from './LaserNestOperatorPanel';
import { LaserNestInfo } from '../../types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { fetchLaserNestDocument: jest.fn() },
}));

const mockApi = api as jest.Mocked<typeof api>;

const baseNest: LaserNestInfo = {
  id: 42,
  nest_name: 'Nest Alpha',
  cnc_number: '7788',
  cnc_file_name: null,
  planned_runs: 6,
  completed_runs: 2,
  remaining_runs: 4,
  material: '304 SS',
  thickness: '0.125"',
  sheet_size: '48x96',
  has_document: false,
};

describe('LaserNestOperatorPanel', () => {
  beforeEach(() => jest.clearAllMocks());

  it('surfaces the CNC number and runs progress', () => {
    render(<LaserNestOperatorPanel nest={baseNest} />);
    expect(screen.getByText(/CNC# 7788/)).toBeInTheDocument();
    // Runs render across nodes ("2 / 6 runs"); assert via the panel's text content.
    const panel = screen.getByTestId('laser-nest-operator-panel');
    expect(panel).toHaveTextContent('2');
    expect(panel).toHaveTextContent('6');
    expect(panel).toHaveTextContent(/runs/i);
    expect(screen.getByText(/304 SS/)).toBeInTheDocument();
  });

  it('does not show a Preview button when there is no attached PDF', () => {
    render(<LaserNestOperatorPanel nest={baseNest} />);
    expect(screen.queryByRole('button', { name: /preview nest/i })).not.toBeInTheDocument();
  });

  it('expands the embedded PDF preview (object URL from a mocked blob) when Preview is tapped', async () => {
    mockApi.fetchLaserNestDocument.mockResolvedValue('blob:operator-nest');
    const { container } = render(
      <LaserNestOperatorPanel
        nest={{ ...baseNest, has_document: true, document_id: 99, document_file_name: 'cut.pdf' }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /preview nest/i }));

    await waitFor(() => expect(mockApi.fetchLaserNestDocument).toHaveBeenCalledWith(42));
    await waitFor(() =>
      expect(container.querySelector('object')).toHaveAttribute('data', 'blob:operator-nest')
    );
  });
});
