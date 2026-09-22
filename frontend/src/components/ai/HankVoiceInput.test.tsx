import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HankVoiceInput } from './HankVoiceInput';

type SpeechResult = { resultIndex: number; results: Array<{ isFinal: boolean; 0: { transcript: string } }> };
class LocalSpeechMock {
  static available = jest.fn<Promise<string>, [{ langs: string[]; processLocally: true }]>();
  static instances: LocalSpeechMock[] = [];
  processLocally = false;
  lang = '';
  continuous = false;
  interimResults = true;
  onresult: ((event: SpeechResult) => void) | null = null;
  onerror: ((event: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  start = jest.fn();
  stop = jest.fn();
  abort = jest.fn();
  constructor() {
    LocalSpeechMock.instances.push(this);
  }
}
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.sig`);
}
function install(value: unknown) {
  Object.defineProperty(window, 'SpeechRecognition', { configurable: true, value });
}
function finalWords(engine: LocalSpeechMock, transcript = 'Report four good parts') {
  act(() =>
    engine.onresult?.({
      resultIndex: 0,
      results: [
        { isFinal: false, 0: { transcript: 'ignore interim' } },
        { isFinal: true, 0: { transcript } },
      ],
    })
  );
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  LocalSpeechMock.instances = [];
  Object.defineProperty(LocalSpeechMock.prototype, 'processLocally', {
    configurable: true,
    value: false,
    writable: true,
  });
  LocalSpeechMock.available.mockResolvedValue('available');
  install(LocalSpeechMock);
});
afterEach(() => {
  install(undefined);
});

it('requires verified local speech support and never falls back to cloud recognition', () => {
  class CloudOnly {
    start = jest.fn();
  }
  install(CloudOnly);
  const onTranscript = jest.fn();
  render(<HankVoiceInput onTranscript={onTranscript} />);
  expect(screen.getByRole('button', { name: 'Hold to talk locally' })).toBeDisabled();
  expect(screen.getByText(/Local voice input is unavailable/)).toBeInTheDocument();
  expect(onTranscript).not.toHaveBeenCalled();
});

it('dictates final words locally while held and stops on release without submitting anything', async () => {
  const onTranscript = jest.fn();
  render(<HankVoiceInput onTranscript={onTranscript} />);
  const button = screen.getByRole('button', { name: 'Hold to talk locally' });
  fireEvent.pointerDown(button);
  await screen.findByText('Listening on this device. Release to finish.');
  expect(LocalSpeechMock.available).toHaveBeenCalledWith({ langs: ['en-US'], processLocally: true });
  const engine = LocalSpeechMock.instances[0];
  expect(engine.processLocally).toBe(true);
  expect(engine.start).toHaveBeenCalledTimes(1);
  finalWords(engine);
  expect(onTranscript).toHaveBeenCalledWith('Report four good parts');
  expect(onTranscript).toHaveBeenCalledTimes(1);
  fireEvent.pointerUp(button);
  expect(engine.stop).toHaveBeenCalledTimes(1);
  act(() => engine.onend?.());
  expect(screen.getByText('Review your words before sending.')).toBeInTheDocument();
});

it('does not start a microphone after release while availability is still pending', async () => {
  let resolve!: (value: string) => void;
  LocalSpeechMock.available.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  render(<HankVoiceInput onTranscript={jest.fn()} />);
  const button = screen.getByRole('button', { name: 'Hold to talk locally' });
  fireEvent.pointerDown(button);
  fireEvent.pointerUp(button);
  await act(async () => resolve('available'));
  expect(LocalSpeechMock.instances).toHaveLength(0);
});

it('refuses missing local language support', async () => {
  LocalSpeechMock.available.mockResolvedValue('downloadable');
  render(<HankVoiceInput onTranscript={jest.fn()} />);
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Hold to talk locally' }));
  await screen.findByText(/Local English speech is not installed/);
  expect(LocalSpeechMock.instances).toHaveLength(0);
});

it('aborts on company change and suppresses any late transcript', async () => {
  const onTranscript = jest.fn();
  render(<HankVoiceInput onTranscript={onTranscript} />);
  fireEvent.keyDown(screen.getByRole('button', { name: 'Hold to talk locally' }), { key: ' ' });
  await waitFor(() => expect(LocalSpeechMock.instances).toHaveLength(1));
  const engine = LocalSpeechMock.instances[0];
  session(5);
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  expect(engine.abort).toHaveBeenCalledTimes(1);
  finalWords(engine);
  expect(onTranscript).not.toHaveBeenCalled();
  expect(screen.getByText(/session changed/)).toBeInTheDocument();
});

it('aborts dictation on unmount and does not append late results', async () => {
  const onTranscript = jest.fn();
  const view = render(<HankVoiceInput onTranscript={onTranscript} />);
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Hold to talk locally' }));
  await waitFor(() => expect(LocalSpeechMock.instances).toHaveLength(1));
  const engine = LocalSpeechMock.instances[0];
  view.unmount();
  expect(engine.abort).toHaveBeenCalledTimes(1);
  finalWords(engine);
  expect(onTranscript).not.toHaveBeenCalled();
});
