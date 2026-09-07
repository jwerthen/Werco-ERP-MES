import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import LazyLaserNestImportWizard from './LazyLaserNestImportWizard';

let mockModuleLoads = 0;
jest.mock('./LaserNestImportWizard', () => {
  mockModuleLoads += 1;
  if (mockModuleLoads === 1) throw new Error('Synthetic chunk download failed');
  return { __esModule: true, default: () => <p>Import tools recovered</p> };
});
jest.mock('../../services/errorLogging', () => ({ logError: jest.fn() }));

it('contains a failed download in the modal and creates a fresh lazy attempt on retry', async () => {
  const error = jest.spyOn(console, 'error').mockImplementation(() => {});
  try {
    render(
      <>
        <h1>Work Orders remains available</h1>
        <LazyLaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />
      </>
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('import tools could not load');
    expect(screen.getByRole('heading', { name: 'Work Orders remains available' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading' }));
    expect(await screen.findByText('Import tools recovered')).toBeInTheDocument();
    expect(mockModuleLoads).toBe(2);
  } finally {
    error.mockRestore();
  }
});
