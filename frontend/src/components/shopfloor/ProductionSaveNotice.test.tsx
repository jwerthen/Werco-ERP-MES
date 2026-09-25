import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import ProductionSaveNotice from './ProductionSaveNotice';

test('uncertain production shows original quantity and separate explicit recovery control', () => {
  const retry = jest.fn();
  render(<ProductionSaveNotice phase="not-confirmed" message="Check original report." online unconfirmed={{ operationId: 31, body: { request_id: 'original-report', quantity_complete_delta: 3, quantity_scrapped_delta: 1 } }} onRetry={retry} />);
  expect(screen.getByRole('alert')).toHaveTextContent('Not confirmed');
  expect(screen.getByRole('alert')).toHaveTextContent('3 complete · 1 scrap');
  fireEvent.click(screen.getByRole('button', { name: 'Check original report' }));
  expect(retry).toHaveBeenCalledTimes(1);
});

test('offline recovery is disabled and successful save announced as status', () => {
  const props = { message: '', online: false, unconfirmed: { operationId: 31, body: { request_id: 'original-report', quantity_complete_delta: 3 } }, onRetry: jest.fn() };
  const view = render(<ProductionSaveNotice {...props} phase="not-confirmed" />);
  expect(screen.getByRole('button', { name: 'Check original report' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('Offline');
  view.rerender(<ProductionSaveNotice {...props} phase="saved" message="Saved" online unconfirmed={null} />);
  expect(screen.getByRole('status')).toHaveTextContent('Saved');
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

test('uncertain correction requires supervisor review and never offers receipt retry', () => {
  const review = jest.fn();
  render(<ProductionSaveNotice phase="not-confirmed" message="Review correction history with your supervisor." online unconfirmed={null} unconfirmedCorrection={{ operationId: 31, submittedAt: '2026-09-25T12:00:00Z', body: { quantity_delta: 2, reason: 'Entered twice' } }} onRetry={jest.fn()} onReviewCorrection={review} />);
  expect(screen.getByRole('alert')).toHaveTextContent('Remove 2 complete');
  expect(screen.getByRole('alert')).toHaveTextContent('cannot be retried safely');
  expect(screen.queryByRole('button', { name: 'Check original report' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Correction reviewed with supervisor' }));
  expect(review).toHaveBeenCalledTimes(1);
});
