import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { CopilotPanel } from './CopilotPanel';
import { CopilotChatResponse, CopilotStreamHandlers } from '../../types/copilot';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    copilotChatStream: jest.fn(),
    copilotChat: jest.fn(),
  },
}));

const api = require('../../services/api').default as {
  copilotChatStream: jest.Mock;
  copilotChat: jest.Mock;
};

const finalResponse: CopilotChatResponse = {
  answer: 'WO-1001 is in progress at Laser 1, due Friday.',
  references: [{ type: 'work_order', id: 7, label: 'WO-1001', url: '/work-orders/7' }],
  tool_trace: [{ tool: 'lookup_work_order', summary: 'looked up WO-1001' }],
  interaction_id: 99,
  rounds: 1,
  truncated: false,
};

function renderPanel(props: Partial<React.ComponentProps<typeof CopilotPanel>> = {}) {
  const onClose = jest.fn();
  const utils = render(
    <MemoryRouter>
      <CopilotPanel isOpen onClose={onClose} {...props} />
    </MemoryRouter>
  );
  return { onClose, ...utils };
}

async function sendMessage(text: string) {
  fireEvent.change(screen.getByLabelText('Ask the copilot'), { target: { value: text } });
  fireEvent.click(screen.getByLabelText('Send message'));
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('CopilotPanel', () => {
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
    expect(screen.getByText('Werco Copilot')).toBeInTheDocument();
  });

  it('calls onClose from the close button and backdrop', () => {
    const { onClose } = renderPanel();
    fireEvent.click(screen.getByLabelText('Close copilot'));
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
      return new Promise<CopilotChatResponse>((resolve) => {
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

  it('disables send while a turn is in flight', async () => {
    api.copilotChatStream.mockImplementation(
      () =>
        new Promise<CopilotChatResponse>(() => {
          /* never resolves */
        })
    );
    renderPanel();
    await sendMessage('slow question');

    fireEvent.change(screen.getByLabelText('Ask the copilot'), { target: { value: 'second question' } });
    expect(screen.getByLabelText('Send message')).toBeDisabled();
    expect(api.copilotChatStream).toHaveBeenCalledTimes(1);
  });
});
