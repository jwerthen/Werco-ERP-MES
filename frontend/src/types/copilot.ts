/**
 * Werco Copilot chat contracts — mirror backend/app/schemas/copilot.py.
 *
 * Conversation state is CLIENT-held (memory only): every request carries the
 * full message history and the server is stateless between turns.
 */

export interface CopilotMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface CopilotChatRequest {
  messages: CopilotMessage[];
  context_hint?: string | null;
}

export interface CopilotReference {
  type: string;
  id: number;
  label: string;
  url: string;
}

export interface CopilotToolTraceEntry {
  tool: string;
  summary: string;
}

export interface CopilotChatResponse {
  answer: string;
  references: CopilotReference[];
  tool_trace: CopilotToolTraceEntry[];
  interaction_id: number | null;
  rounds: number;
  truncated: boolean;
}

/** One frame of the SSE stream (`data: <json>`). */
export type CopilotStreamEvent =
  | { type: 'tool_use'; tool: string; summary: string }
  | { type: 'delta'; text: string }
  | ({ type: 'final' } & CopilotChatResponse)
  | { type: 'error'; message: string };

export interface CopilotStreamHandlers {
  onToolUse?: (tool: string, summary: string) => void;
  onDelta?: (text: string) => void;
  onFinal?: (response: CopilotChatResponse) => void;
  onError?: (message: string) => void;
}
