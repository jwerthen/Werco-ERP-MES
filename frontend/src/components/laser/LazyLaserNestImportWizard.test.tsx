import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import LazyLaserNestImportWizard from './LazyLaserNestImportWizard';

let mockModuleLoads = 0;
jest.mock('./LaserNestImportWizard', () => {
  mockModuleLoads += 1;
  return {
    __esModule: true,
    default: ({ onImported, workOrderId }: { onImported: (id: number) => void; workOrderId?: number }) => (
      <button onClick={() => onImported(workOrderId || 42)}>Import tools ready</button>
    ),
  };
});

it('does not load the workflow while closed and preserves import callbacks when opened', async () => {
  const onClose = jest.fn();
  const onImported = jest.fn();
  const view = render(
    <LazyLaserNestImportWizard open={false} onClose={onClose} onImported={onImported} workOrderId={73} />
  );
  expect(mockModuleLoads).toBe(0);
  view.rerender(<LazyLaserNestImportWizard open onClose={onClose} onImported={onImported} workOrderId={73} />);
  expect(screen.getByRole('status')).toHaveTextContent('Loading nest import…');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(onClose).toHaveBeenCalledTimes(1);
  fireEvent.click(await screen.findByRole('button', { name: 'Import tools ready' }));
  expect(mockModuleLoads).toBe(1);
  expect(onImported).toHaveBeenCalledWith(73);
});
