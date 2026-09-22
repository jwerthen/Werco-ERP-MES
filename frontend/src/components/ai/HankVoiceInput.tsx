import React, { useEffect, useRef, useState } from 'react';
import { MicrophoneIcon } from '@heroicons/react/24/outline';
import { getHankSessionScope, subscribeHankSession } from './hankSession';

interface LocalSpeech {
  processLocally: boolean;
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult:
    | ((event: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void)
    | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
interface LocalSpeechConstructor {
  new (): LocalSpeech;
  prototype: LocalSpeech;
  available(options: { langs: string[]; processLocally: true }): Promise<string>;
}
function localSpeechConstructor(): LocalSpeechConstructor | undefined {
  const browser = window as unknown as {
    SpeechRecognition?: LocalSpeechConstructor;
    webkitSpeechRecognition?: LocalSpeechConstructor;
  };
  const ctor = browser.SpeechRecognition || browser.webkitSpeechRecognition;
  return ctor && typeof ctor.available === 'function' && 'processLocally' in ctor.prototype ? ctor : undefined;
}

/** Dictates into the editable composer. Never sends audio or starts a chat turn. */
export function HankVoiceInput({
  onTranscript,
  disabled = false,
}: {
  onTranscript: (text: string) => void;
  disabled?: boolean;
}) {
  const [state, setState] = useState<'idle' | 'checking' | 'listening'>('idle');
  const [message, setMessage] = useState('');
  const [supported] = useState(() => !!localSpeechConstructor());
  const [scope] = useState(getHankSessionScope);
  const mounted = useRef(true);
  const held = useRef(false);
  const generation = useRef(0);
  const recognition = useRef<LocalSpeech | null>(null);
  const transcriptCallback = useRef(onTranscript);
  transcriptCallback.current = onTranscript;
  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (scope !== getHankSessionScope()) {
        generation.current++;
        held.current = false;
        recognition.current?.abort();
        setState('idle');
        setMessage('Your session changed. Reopen Hank to dictate.');
      }
    });
    return () => {
      mounted.current = false;
      held.current = false;
      generation.current++;
      recognition.current?.abort();
      unsubscribe();
    };
  }, [scope]);
  useEffect(() => {
    if (disabled) {
      generation.current++;
      held.current = false;
      recognition.current?.abort();
      setState('idle');
    }
  }, [disabled]);
  const start = async () => {
    if (disabled || held.current || scope !== getHankSessionScope() || !scope) return;
    const Speech = localSpeechConstructor();
    if (!Speech) return;
    held.current = true;
    const turn = ++generation.current;
    setState('checking');
    setMessage('Checking local speech support…');
    const current = () => mounted.current && turn === generation.current && scope === getHankSessionScope();
    try {
      const availability = await Speech.available({ langs: ['en-US'], processLocally: true });
      if (!current() || !held.current) return;
      if (availability !== 'available') {
        held.current = false;
        setState('idle');
        setMessage('Local English speech is not installed or available. Type or scan instead.');
        return;
      }
      const engine = new Speech();
      engine.processLocally = true;
      if (engine.processLocally !== true) throw new Error('Local speech required');
      engine.lang = 'en-US';
      engine.continuous = true;
      engine.interimResults = false;
      engine.onresult = event => {
        if (!current()) return;
        const text: string[] = [];
        for (let index = event.resultIndex; index < event.results.length; index++)
          if (event.results[index].isFinal) text.push(event.results[index][0].transcript);
        if (text.length) transcriptCallback.current(text.join(' ').trim());
      };
      engine.onerror = () => {
        if (current()) {
          held.current = false;
          setState('idle');
          setMessage('Local dictation stopped. Check microphone access, or type instead.');
        }
      };
      engine.onend = () => {
        if (current()) {
          held.current = false;
          recognition.current = null;
          setState('idle');
          setMessage('Review your words before sending.');
        }
      };
      recognition.current = engine;
      engine.start();
      setState('listening');
      setMessage('Listening on this device. Release to finish.');
    } catch {
      if (current()) {
        held.current = false;
        setState('idle');
        setMessage('Local dictation is unavailable. Type or scan instead.');
      }
    }
  };
  const stop = () => {
    held.current = false;
    recognition.current?.stop();
    if (!recognition.current) {
      generation.current++;
      setState('idle');
      setMessage('Hold to speak when local dictation is ready.');
    }
  };
  return (
    <div className="space-y-1">
      <button
        type="button"
        disabled={disabled || !supported}
        aria-label="Hold to talk locally"
        aria-pressed={state === 'listening'}
        className="inline-flex items-center gap-1.5 text-xs text-fd-mute disabled:opacity-50 touch-none"
        onPointerDown={event => {
          event.currentTarget.setPointerCapture?.(event.pointerId);
          void start();
        }}
        onPointerUp={stop}
        onPointerCancel={stop}
        onKeyDown={event => {
          if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
            event.preventDefault();
            void start();
          }
        }}
        onKeyUp={event => {
          if (event.key === ' ' || event.key === 'Enter') {
            event.preventDefault();
            stop();
          }
        }}
      >
        <MicrophoneIcon className="h-4 w-4" />
        {state === 'listening' ? 'Listening…' : 'Hold to talk'}
      </button>
      <p className="text-[10px] text-fd-mute" role={message ? 'status' : undefined}>
        {supported
          ? message || 'Local dictation only. Review the text before sending.'
          : 'Local voice input is unavailable in this browser. Type or scan instead.'}
      </p>
    </div>
  );
}
