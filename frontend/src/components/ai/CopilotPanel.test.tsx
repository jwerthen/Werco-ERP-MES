import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { CopilotPanel } from './CopilotPanel';
import { CopilotChatResponse, CopilotStreamHandlers } from '../../types/copilot';
import type { UserRole } from '../../types';
import type { HankBriefing } from '../../types/hank';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    copilotChatStream: jest.fn(),
    copilotChat: jest.fn(),
    getDocumentTypes: jest.fn(),
    getWorkOrder: jest.fn(),
    uploadDocument: jest.fn(),
    getHankBriefing: jest.fn(),
  },
}));

const api = require('../../services/api').default as {
  copilotChatStream: jest.Mock;
  copilotChat: jest.Mock;
  getDocumentTypes: jest.Mock;
  getWorkOrder: jest.Mock;
  uploadDocument: jest.Mock;
  getHankBriefing: jest.Mock;
};

let mockRole: UserRole = 'quality';
jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));

const finalResponse: CopilotChatResponse = {
  answer: 'WO-1001 is in progress at Laser 1, due Friday.',
  references: [{ type: 'work_order', id: 7, label: 'WO-1001', url: '/work-orders/7' }],
  tool_trace: [{ tool: 'lookup_work_order', summary: 'looked up WO-1001' }],
  interaction_id: 99,
  rounds: 1,
  truncated: false,
};

const shiftBriefing: HankBriefing = {
  checked_at: '2026-09-22T13:30:00Z',
  role: 'quality',
  headline: 'Your quality shift',
  summary: 'Review your assigned inspection records.',
  sections: [],
  coverage_notes: [],
};

function renderPanel(props: Partial<React.ComponentProps<typeof CopilotPanel>> = {}, pathname = '/') {
  const onClose = jest.fn();
  const utils = render(
    <MemoryRouter initialEntries={[pathname]}>
      <CopilotPanel isOpen onClose={onClose} {...props} />
    </MemoryRouter>
  );
  return { onClose, ...utils };
}

async function sendMessage(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Ask Hank'), { target: { value: text } });
    fireEvent.click(screen.getByLabelText('Send message'));
  });
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  mockRole = 'quality';
  api.copilotChatStream.mockResolvedValue(finalResponse);
  api.getDocumentTypes.mockResolvedValue([{ value: 'drawing', label: 'Drawing' }]);
  api.getWorkOrder.mockResolvedValue({ id: 7, work_order_number: 'WO-1007' });
  api.getHankBriefing.mockResolvedValue(shiftBriefing);
});

describe('CopilotPanel', () => {
  it('loads My shift on demand and retains the chat conversation when returning from the briefing', async () => {
    renderPanel();
    expect(api.getHankBriefing).not.toHaveBeenCalled();
    await sendMessage('where is WO-1001?');
    await screen.findByText(finalResponse.answer);
    fireEvent.click(screen.getByRole('button', { name: 'My shift' }));
    expect(await screen.findByRole('heading', { name: 'Your quality shift' })).toBeInTheDocument();
    expect(api.getHankBriefing).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'My shift' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByLabelText('Ask Hank')).not.toBeInTheDocument();
    expect(screen.queryByText(finalResponse.answer)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Chat' }));
    expect(screen.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(finalResponse.answer)).toBeInTheDocument();
    expect(screen.getByTestId('copilot-message-user')).toHaveTextContent('where is WO-1001?');
    expect(screen.queryByRole('region', { name: 'My shift briefing' })).not.toBeInTheDocument();
    await sendMessage('what happens next?');
    expect(api.copilotChatStream.mock.calls[1][0].messages).toEqual([
      { role: 'user', content: 'where is WO-1001?' },
      { role: 'assistant', content: finalResponse.answer },
      { role: 'user', content: 'what happens next?' },
    ]);
  });

  it('is hidden when closed and visible when open', () => {
    const { rerender } = render(
      <MemoryRouter>
        <CopilotPanel isOpen={false} onClose={jest.fn()} />
      </MemoryRouter>
    );
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('aria-hidden', 'true');

    rerender(
      <MemoryRouter>
        <CopilotPanel isOpen onClose={jest.fn()} />
      </MemoryRouter>
    );
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-hidden', 'false');
    expect(screen.getByRole('heading', { name: 'Hank' })).toBeInTheDocument();
    expect(screen.getByText('AI shop teammate')).toBeInTheDocument();
  });

  it('calls onClose from the close button and backdrop', () => {
    const { onClose } = renderPanel();
    fireEvent.click(screen.getByLabelText('Close Hank'));
    fireEvent.click(screen.getByTestId('copilot-backdrop'));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('sends a message and renders the streamed answer with deep links', async () => {
    api.copilotChatStream.mockImplementation(async (_request, handlers: CopilotStreamHandlers) => {
      handlers.onToolUse?.('lookup_work_order', 'looked up WO-1001');
      handlers.onDelta?.('WO-1001 is in progress ');
      handlers.onDelta?.('at Laser 1, due Friday.');
      handlers.onFinal?.(finalResponse);
      return finalResponse;
    });

    renderPanel();
    await sendMessage('where is WO-1001?');

    expect(screen.getByTestId('copilot-message-user')).toHaveTextContent('where is WO-1001?');
    await waitFor(() => {
      expect(screen.getByText('WO-1001 is in progress at Laser 1, due Friday.')).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: 'WO-1001' });
    expect(link).toHaveAttribute('href', '/work-orders/7');
    expect(screen.getByText('looked up WO-1001')).toBeInTheDocument(); // tool-activity hint line

    const request = api.copilotChatStream.mock.calls[0][0];
    expect(request.messages).toEqual([{ role: 'user', content: 'where is WO-1001?' }]);
    expect(request.context_hint).toContain('viewing /');
  });

  it('renders streaming tokens progressively before the final frame', async () => {
    let capturedHandlers: CopilotStreamHandlers = {};
    let finish: (value: CopilotChatResponse) => void = () => undefined;
    api.copilotChatStream.mockImplementation((_request, handlers: CopilotStreamHandlers) => {
      capturedHandlers = handlers;
      return new Promise<CopilotChatResponse>(resolve => {
        finish = resolve;
      });
    });

    renderPanel();
    await sendMessage('anything blocked?');

    act(() => {
      capturedHandlers.onToolUse?.('list_blocked_work_orders', 'found 2 open blockers');
    });
    expect(screen.getByTestId('copilot-activity')).toHaveTextContent('found 2 open blockers');

    act(() => {
      capturedHandlers.onDelta?.('Two jobs are blocked: ');
      capturedHandlers.onDelta?.('WO-7 and WO-9.');
    });
    expect(screen.getByTestId('copilot-streaming')).toHaveTextContent('Two jobs are blocked: WO-7 and WO-9.');

    await act(async () => {
      finish({ ...finalResponse, answer: 'Two jobs are blocked: WO-7 and WO-9.', references: [], tool_trace: [] });
    });
    await waitFor(() => {
      expect(screen.queryByTestId('copilot-streaming')).not.toBeInTheDocument();
      expect(screen.getByTestId('copilot-message-assistant')).toHaveTextContent('Two jobs are blocked: WO-7 and WO-9.');
    });
  });

  it('shows an error bubble with retry, and retry resends the same question', async () => {
    api.copilotChatStream.mockRejectedValueOnce(new Error('Copilot request failed (502)'));
    api.copilotChatStream.mockImplementationOnce(async (_request, handlers: CopilotStreamHandlers) => {
      handlers.onFinal?.(finalResponse);
      return finalResponse;
    });

    renderPanel();
    await sendMessage('where is WO-1001?');

    await waitFor(() => {
      expect(screen.getByText('Copilot request failed (502)')).toBeInTheDocument();
    });
    expect(screen.getByText(/Last request failed/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText(finalResponse.answer)).toBeInTheDocument();
    });

    expect(api.copilotChatStream).toHaveBeenCalledTimes(2);
    const retryRequest = api.copilotChatStream.mock.calls[1][0];
    expect(retryRequest.messages).toEqual([{ role: 'user', content: 'where is WO-1001?' }]);
    // The failed assistant bubble is not resent as history.
    expect(retryRequest.messages.some((m: { role: string }) => m.role === 'assistant')).toBe(false);
  });

  it('keeps history in memory across close/open (component stays mounted)', async () => {
    api.copilotChatStream.mockImplementation(async (_request, handlers: CopilotStreamHandlers) => {
      handlers.onFinal?.(finalResponse);
      return finalResponse;
    });

    const onClose = jest.fn();
    const { rerender } = render(
      <MemoryRouter>
        <CopilotPanel isOpen onClose={onClose} />
      </MemoryRouter>
    );
    await sendMessage('where is WO-1001?');
    await waitFor(() => expect(screen.getByText(finalResponse.answer)).toBeInTheDocument());

    rerender(
      <MemoryRouter>
        <CopilotPanel isOpen={false} onClose={onClose} />
      </MemoryRouter>
    );
    rerender(
      <MemoryRouter>
        <CopilotPanel isOpen onClose={onClose} />
      </MemoryRouter>
    );
    expect(screen.getByText(finalResponse.answer)).toBeInTheDocument();
  });

  it('offers stop and prevents another turn while one is in flight', async () => {
    api.copilotChatStream.mockImplementation(
      () =>
        new Promise<CopilotChatResponse>(() => {
          /* never resolves */
        })
    );
    renderPanel();
    await sendMessage('slow question');

    fireEvent.change(screen.getByLabelText('Ask Hank'), { target: { value: 'second question' } });
    expect(screen.queryByLabelText('Send message')).not.toBeInTheDocument();
    expect(screen.getByLabelText("Stop Hank's answer")).toBeEnabled();
    fireEvent.keyDown(screen.getByLabelText('Ask Hank'), { key: 'Enter' });
    expect(api.copilotChatStream).toHaveBeenCalledTimes(1);
  });

  it.each<UserRole>(['admin', 'manager', 'quality', 'platform_admin'])('offers PDF filing to %s', role => {
    mockRole = role;
    renderPanel();
    expect(screen.getByRole('button', { name: 'Upload PDF' })).toBeInTheDocument();
  });

  it.each<UserRole>(['operator', 'supervisor', 'shipping', 'viewer'])('keeps PDF filing unavailable to %s', role => {
    mockRole = role;
    renderPanel();
    expect(screen.queryByRole('button', { name: 'Upload PDF' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask Hank')).toBeEnabled();
  });

  it('hides publishing in a read-only administrator session', () => {
    mockRole = 'admin';
    sessionStorage.setItem(
      'token',
      `header.${btoa(JSON.stringify({ sub: '1', cid: 4, ro: true, type: 'access' }))}.signature`
    );
    renderPanel();
    expect(screen.queryByRole('button', { name: 'Upload PDF' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask Hank')).toBeEnabled();
  });

  it('uses the current work-order ID in its contextual briefing and request context', async () => {
    renderPanel({}, '/work-orders/7?tab=operations');
    fireEvent.click(screen.getByRole('button', { name: 'Brief me on this job' }));
    await screen.findByText(finalResponse.answer);
    expect(api.copilotChatStream.mock.calls[0][0]).toEqual({
      messages: [
        {
          role: 'user',
          content:
            'Look up work order ID 7. Summarize its status, operations, blockers, and recent activity, with a link to the job.',
        },
      ],
      context_hint: 'viewing /work-orders/7?tab=operations',
    });
  });

  it('prepares an inventory question for the employee to finish before querying', () => {
    renderPanel({}, '/inventory');
    fireEvent.click(screen.getByRole('button', { name: 'Check stock for a part…' }));
    expect(screen.getByLabelText('Ask Hank')).toHaveValue('Check available inventory, locations, and lots for part ');
    expect(screen.getByLabelText('Ask Hank')).toHaveFocus();
    expect(api.copilotChatStream).not.toHaveBeenCalled();
  });

  it('files a PDF from Documents and shows the server receipt and link only after confirmation', async () => {
    let finish!: (document: { id: number; document_number: string; title: string; revision: string }) => void;
    api.uploadDocument.mockImplementation(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    renderPanel({}, '/documents');
    fireEvent.click(screen.getByRole('button', { name: 'Upload PDF' }));
    await screen.findByRole('option', { name: 'Drawing' });
    const file = new File(['%PDF-1.7'], 'inspection.pdf', { type: 'application/pdf' });
    fireEvent.change(screen.getByLabelText(/PDF file/), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText(/Document type/), { target: { value: 'drawing' } });
    fireEvent.click(screen.getByRole('button', { name: 'Upload and release PDF' }));
    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/The PDF is saved and released/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /DOC-0042/ })).not.toBeInTheDocument();
    expect(api.copilotChatStream).not.toHaveBeenCalled();
    expect(api.uploadDocument.mock.calls[0][0].get('file')).toEqual(file);
    const close = screen.getByLabelText('Close Hank');
    close.focus();
    fireEvent.keyDown(close, { key: 'Tab' });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(close).toHaveFocus();

    await act(async () => finish({ id: 42, document_number: 'DOC-0042', title: 'inspection', revision: 'A' }));
    expect(screen.getByTestId('copilot-message-assistant')).toHaveTextContent(
      'Filed inspection as DOC-0042, revision A. The PDF is saved and released in Documents.'
    );
    expect(screen.getByRole('link', { name: 'DOC-0042 · Rev A' })).toHaveAttribute('href', '/documents?document=42');
    expect(screen.queryByRole('form', { name: 'File a PDF with Hank' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Ask Hank')).toBeEnabled();
    await sendMessage('Where did you file it?');
    await screen.findByText(finalResponse.answer);
    expect(api.copilotChatStream.mock.calls[0][0].messages).toEqual([
      { role: 'user', content: 'File PDF: inspection' },
      {
        role: 'assistant',
        content: 'Filed inspection as DOC-0042, revision A. The PDF is saved and released in Documents.',
      },
      { role: 'user', content: 'Where did you file it?' },
    ]);
  });

  it('keeps page context within the API limit when the URL has a long query', async () => {
    renderPanel({}, `/documents?filter=${'x'.repeat(1000)}`);
    await sendMessage('brief me');
    await screen.findByText(finalResponse.answer);
    const request = api.copilotChatStream.mock.calls[0][0];
    expect(request.context_hint).toHaveLength(500);
    expect(request.context_hint).toMatch(/^viewing \/documents\?filter=/);
  });

  it('restores the conversation after an employee cancels a PDF form without submitting', async () => {
    renderPanel();
    await sendMessage('where is WO-1001?');
    await screen.findByText(finalResponse.answer);
    fireEvent.click(screen.getByRole('button', { name: 'Upload PDF' }));
    await screen.findByRole('option', { name: 'Drawing' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel upload' }));
    expect(screen.getByText(finalResponse.answer)).toBeInTheDocument();
    expect(screen.getByLabelText('Ask Hank')).toBeEnabled();
    expect(api.uploadDocument).not.toHaveBeenCalled();
  });

  it('aborts on Stop and ignores a late answer and activity callbacks', async () => {
    let finish!: (value: CopilotChatResponse) => void;
    let handlers: CopilotStreamHandlers = {};
    let signal: AbortSignal | undefined;
    api.copilotChatStream.mockImplementation(
      (_request, nextHandlers: CopilotStreamHandlers, nextSignal: AbortSignal) => {
        handlers = nextHandlers;
        signal = nextSignal;
        return new Promise<CopilotChatResponse>(resolve => {
          finish = resolve;
        });
      }
    );
    renderPanel();
    await sendMessage('slow question');
    fireEvent.click(screen.getByLabelText("Stop Hank's answer"));
    expect(signal?.aborted).toBe(true);
    act(() => {
      handlers.onToolUse?.('lookup_work_order', 'stale activity');
      handlers.onDelta?.('stale text');
    });
    await act(async () => finish(finalResponse));
    expect(screen.queryByText('stale activity')).not.toBeInTheDocument();
    expect(screen.queryByText('stale text')).not.toBeInTheDocument();
    expect(screen.queryByText(finalResponse.answer)).not.toBeInTheDocument();
    expect(screen.queryByTestId('copilot-streaming')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Send message')).toBeInTheDocument();
  });

  it('clears a cancelled turn and keeps the new stream busy when the old request finally resolves', async () => {
    let finishOld!: (value: CopilotChatResponse) => void;
    let finishNew!: (value: CopilotChatResponse) => void;
    let newHandlers: CopilotStreamHandlers = {};
    let oldSignal: AbortSignal | undefined;
    api.copilotChatStream.mockImplementationOnce((_request, _handlers, signal: AbortSignal) => {
      oldSignal = signal;
      return new Promise<CopilotChatResponse>(resolve => {
        finishOld = resolve;
      });
    });
    api.copilotChatStream.mockImplementationOnce((_request, handlers: CopilotStreamHandlers) => {
      newHandlers = handlers;
      return new Promise<CopilotChatResponse>(resolve => {
        finishNew = resolve;
      });
    });
    renderPanel();
    await sendMessage('first question');
    fireEvent.click(screen.getByLabelText('Clear conversation'));
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.queryByText('first question')).not.toBeInTheDocument();
    await sendMessage('new question');
    act(() => newHandlers.onDelta?.('Fresh answer '));
    await act(async () => finishOld(finalResponse));
    expect(screen.getByTestId('copilot-streaming')).toHaveTextContent('Fresh answer');
    expect(screen.getByLabelText("Stop Hank's answer")).toBeInTheDocument();
    expect(screen.queryByText(finalResponse.answer)).not.toBeInTheDocument();
    expect(api.copilotChatStream.mock.calls[1][0].messages).toEqual([{ role: 'user', content: 'new question' }]);
    await act(async () => finishNew({ ...finalResponse, answer: 'Fresh answer complete.' }));
    expect(screen.getByTestId('copilot-message-assistant')).toHaveTextContent('Fresh answer complete.');
    expect(screen.queryByTestId('copilot-streaming')).not.toBeInTheDocument();
  });
});

it('removes the closed drawer from keyboard use and restores focus when closed', () => {
  jest.useFakeTimers();
  const close = jest.fn();
  const trigger = document.createElement('button');
  document.body.appendChild(trigger);
  trigger.focus();
  const { rerender } = render(
    <MemoryRouter>
      <CopilotPanel isOpen={false} onClose={close} />
    </MemoryRouter>
  );
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('inert');
  rerender(
    <MemoryRouter>
      <CopilotPanel isOpen onClose={close} />
    </MemoryRouter>
  );
  act(() => jest.advanceTimersByTime(150));
  expect(screen.getByLabelText('Ask Hank')).toHaveFocus();
  fireEvent.keyDown(screen.getByLabelText('Ask Hank'), { key: 'Escape' });
  expect(close).toHaveBeenCalledTimes(1);
  rerender(
    <MemoryRouter>
      <CopilotPanel isOpen={false} onClose={close} />
    </MemoryRouter>
  );
  expect(trigger).toHaveFocus();
  trigger.remove();
  jest.useRealTimers();
});
