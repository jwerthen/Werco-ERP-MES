import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import { CopilotPanel } from './CopilotPanel';
import type { HankDocumentIntake } from './HankDocumentIntake';

jest.mock('../../services/api', () => ({ __esModule: true, default: { copilotChatStream: jest.fn() } }));
jest.mock('../../hooks/usePermissions', () => ({ usePermissions: () => ({ role: 'manager', isSuperuser: false }) }));
jest.mock('./HankVoiceInput', () => ({ HankVoiceInput: () => null }));
jest.mock('./HankDocumentIntake', () => ({
  HankDocumentIntake: ({ onUseInChat }: React.ComponentProps<typeof HankDocumentIntake>) => (
    <>
      {[1, 2, 3, 4, 5, 6].map(id => (
        <button
          key={id}
          onClick={() =>
            onUseInChat?.({
              id,
              filename: `delivery-${id}.pdf`,
              company_id: 4,
              status: 'awaiting_review',
              analysis: { summary: 'EXTRACTED DATA STAYS ON SERVER' },
            } as Parameters<NonNullable<React.ComponentProps<typeof HankDocumentIntake>['onUseInChat']>>[0])
          }
        >
          Use delivery {id}
        </button>
      ))}
    </>
  ),
}));
const mocked = jest.mocked(api);
const response = {
  answer: 'Review the quantities.',
  references: [],
  tool_trace: [],
  rounds: 1,
  truncated: false,
  interaction_id: 1,
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.sig`);
}
function panel() {
  return render(
    <MemoryRouter>
      <CopilotPanel isOpen onClose={jest.fn()} />
    </MemoryRouter>
  );
}
function attach(id = 1) {
  fireEvent.click(screen.getByRole('button', { name: 'Upload PDFs' }));
  fireEvent.click(screen.getByRole('button', { name: `Use delivery ${id}` }));
}
async function send() {
  fireEvent.change(screen.getByLabelText('Ask Hank'), { target: { value: 'Check the materials received.' } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  mocked.copilotChatStream.mockResolvedValue(response);
});
it('attaches explicit source IDs, retains them across turns, and removes them from later requests', async () => {
  panel();
  attach();
  expect(screen.getByLabelText('PDFs attached to chat')).toHaveTextContent('delivery-1.pdf');
  await send();
  expect(mocked.copilotChatStream.mock.calls[0][0]).toEqual({
    messages: [{ role: 'user', content: 'Check the materials received.' }],
    context_hint: 'viewing /',
    intake_file_ids: [1],
  });
  await send();
  expect(mocked.copilotChatStream.mock.calls[1][0].intake_file_ids).toEqual([1]);
  expect(JSON.stringify(mocked.copilotChatStream.mock.calls)).not.toContain('EXTRACTED DATA');
  fireEvent.click(screen.getByRole('button', { name: 'Remove delivery-1.pdf from chat' }));
  await send();
  expect(mocked.copilotChatStream.mock.calls[2][0].intake_file_ids).toBeUndefined();
});
it('retries with the same document IDs even if the employee changes the next-turn attachment list', async () => {
  mocked.copilotChatStream.mockRejectedValueOnce(new Error('Temporary failure'));
  panel();
  attach();
  await send();
  fireEvent.click(screen.getByRole('button', { name: 'Remove delivery-1.pdf from chat' }));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  });
  expect(mocked.copilotChatStream.mock.calls[1][0].intake_file_ids).toEqual([1]);
});
it('deduplicates attachments, enforces the five-PDF limit, and clears them with the conversation', async () => {
  panel();
  attach();
  attach();
  for (const id of [2, 3, 4, 5]) attach(id);
  attach(6);
  expect(screen.getByRole('alert')).toHaveTextContent('up to 5 PDFs');
  fireEvent.click(screen.getByRole('button', { name: 'Back to chat' }));
  expect(screen.getAllByRole('button', { name: /Remove delivery/ })).toHaveLength(5);
  fireEvent.click(screen.getByRole('button', { name: 'Clear conversation' }));
  expect(screen.queryByLabelText('PDFs attached to chat')).not.toBeInTheDocument();
  await send();
  expect(mocked.copilotChatStream.mock.calls[0][0].intake_file_ids).toBeUndefined();
});
it('does not submit old document attachments after a company switch', async () => {
  panel();
  attach();
  session(9);
  await send();
  expect(mocked.copilotChatStream).not.toHaveBeenCalled();
});
