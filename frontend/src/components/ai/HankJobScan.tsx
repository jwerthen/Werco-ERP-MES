import React, { useState } from 'react';
import api from '../../services/api';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { useHankSessionGuard } from './useHankSessionGuard';

export interface HankScannedJob {
  workOrderId: number;
  operationId?: number;
  label: string;
}

export function HankJobScan({
  onSelect,
  disabled = false,
}: {
  onSelect: (job: HankScannedJob) => void;
  disabled?: boolean;
}) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const { current, controller, release, changed } = useHankSessionGuard();
  const scan = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || disabled || !code.trim() || !current()) return;
    const request = controller();
    setBusy(true);
    setMessage('');
    try {
      const result = await api.resolveScanAction(code.trim(), undefined, request.signal);
      if (!current() || request.signal.aborted) return;
      if (result.kind === 'work_order') {
        onSelect({ workOrderId: result.work_order.id, label: result.work_order.work_order_number });
        setMessage(`Selected ${result.work_order.work_order_number}.`);
      } else if (result.kind === 'operation') {
        onSelect({
          workOrderId: result.operation.work_order_id,
          operationId: result.operation.id,
          label: `${result.operation.work_order_number} · ${result.operation.name}`,
        });
        setMessage(`Selected ${result.operation.work_order_number} · ${result.operation.name}.`);
      } else setMessage('Scan a work-order or operation label to select a job.');
    } catch {
      if (current() && !request.signal.aborted)
        setMessage('That label could not be resolved. Check the code and try again.');
    } finally {
      release(request);
      if (current()) setBusy(false);
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-xs text-fd-amber">
        Your session changed. Reopen Hank to scan a job.
      </p>
    );
  return (
    <form aria-label="Select a job by scan" onSubmit={scan} className="space-y-2">
      <FormField
        label="Scan job or operation label"
        help="Use a keyboard scanner, or type a traveler code and press Enter."
      >
        {field => (
          <input
            {...field}
            className="input w-full"
            value={code}
            maxLength={200}
            autoComplete="off"
            disabled={disabled || busy}
            onChange={event => setCode(event.target.value)}
            placeholder="WO:… or OP:…"
          />
        )}
      </FormField>
      <LoadingButton
        type="submit"
        size="sm"
        disabled={disabled || !code.trim()}
        loading={busy}
        loadingText="Finding job…"
      >
        Select scanned job
      </LoadingButton>
      {message && (
        <p role="status" className="text-xs text-fd-mute">
          {message}
        </p>
      )}
    </form>
  );
}
