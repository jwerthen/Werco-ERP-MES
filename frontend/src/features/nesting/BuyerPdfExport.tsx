import React, { useMemo, useRef, useState, useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import api from '../../services/api';
import { buildBuyerPdfReport, getBuyerPdfChoices } from './lib/buyer-pdf';
import type { BuyerPdfGroupChoices, BuyerPdfInputs } from './lib/buyer-pdf-types';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';

type Props = {
  inputs: BuyerPdfInputs;
  estimatorId?: number;
  disabled: boolean;
  canPlanRemnants: boolean;
};
type Session = { inputs: BuyerPdfInputs; groups: BuyerPdfGroupChoices[]; context: string };
const fields = z.object({
  projectName: z.string().trim().min(1, 'Enter a job or project reference.').max(200),
  notes: z.string().max(2000, 'Use at most 2,000 characters for buyer notes.'),
  choices: z.array(z.string().min(1, 'Choose a complete alternative.')),
});
type Fields = z.infer<typeof fields>;

/** Local dialog state never changes the nest or establishes an order selection. */
export default function BuyerPdfExport(props: Props) {
  const { inputs, estimatorId, disabled, canPlanRemnants } = props;
  const context = `${inputs.companyId}:${estimatorId}:${canPlanRemnants}`;
  const current = useRef({ inputs, context, disabled });
  current.current = { inputs, context, disabled };
  const [session, setSession] = useState<Session | null>(null);
  const activeSession = useRef<Session | null>(null);
  const groups = useMemo(() => {
    try {
      return getBuyerPdfChoices(inputs);
    } catch {
      return [];
    }
  }, [inputs]);
  const ready =
    !disabled &&
    inputs.companyId > 0 &&
    (!inputs.project.remnantPlan || canPlanRemnants) &&
    groups.length > 0 &&
    groups.every(group => group.choices.length > 0);
  const close = () => {
    activeSession.current = null;
    setSession(null);
  };
  const isCurrent = (candidate: Session) =>
    activeSession.current === candidate &&
    current.current.inputs === candidate.inputs &&
    current.current.context === candidate.context &&
    !current.current.disabled;
  const visible = session && ready && session.inputs === inputs && session.context === context;
  useEffect(() => {
    if (session && !visible) {
      activeSession.current = null;
      setSession(null);
    }
  }, [session, visible]);
  return (
    <>
      <button
        className="secondary compact"
        disabled={!ready}
        title={
          ready
            ? 'Choose complete material plans and download nests with quantities'
            : 'Compare current inputs and obtain a complete option for every material group'
        }
        onClick={() => {
          if (!ready) return;
          const next = { inputs, groups, context };
          activeSession.current = next;
          setSession(next);
        }}
      >
        Export buyer PDF
      </button>
      {visible && <BuyerPdfDialog session={session} isCurrent={() => isCurrent(session)} onClose={close} />}
    </>
  );
}

function BuyerPdfDialog({
  session,
  isCurrent,
  onClose,
}: {
  session: Session;
  isCurrent: () => boolean;
  onClose: () => void;
}) {
  const { inputs, groups } = session;
  const form = useForm<Fields>({
    resolver: zodResolver(fields),
    defaultValues: {
      projectName: inputs.project.name,
      notes: '',
      choices: groups.map(group => (group.choices.find(choice => choice.recommended) ?? group.choices[0]).id),
    },
  });
  const selected = form.watch('choices');
  const conditional = groups.some(
    (group, index) => group.choices.find(choice => choice.id === selected[index])?.conditional
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function generate(values: Fields) {
    if (controller.current || !isCurrent()) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setError('');
    setMessage('');
    // Keep the source, every choice and report-only text fixed across awaits.
    const sourceJSON = JSON.stringify(inputs);
    const formJSON = JSON.stringify(form.getValues());
    const valid = () =>
      !request.signal.aborted &&
      isCurrent() &&
      sourceJSON === JSON.stringify(inputs) &&
      formJSON === JSON.stringify(form.getValues());
    try {
      const source = JSON.parse(sourceJSON) as BuyerPdfInputs;
      const selections = Object.fromEntries(groups.map((group, index) => [group.groupId, values.choices[index]]));
      const report = await buildBuyerPdfReport(source, selections, {
        projectName: values.projectName,
        notes: values.notes,
      });
      if (!valid()) return;
      if (report.expectedCompanyId !== inputs.companyId)
        throw new Error('The PDF report belongs to a different company.');
      const pdf = await api.generateNestingBuyerPdf(report, request.signal);
      if (!valid()) return;
      if (
        !(pdf instanceof Blob) ||
        pdf.type.split(';')[0].toLowerCase() !== 'application/pdf' ||
        !pdf.size ||
        pdf.size > 20 * 1024 * 1024
      )
        throw new Error('The server did not return a valid, bounded PDF. Nothing was downloaded.');
      const header = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error('The PDF could not be read. Nothing was downloaded.'));
        reader.readAsText(pdf.slice(0, 5));
      });
      if (!valid()) return;
      if (header !== '%PDF-') throw new Error('The response is not a PDF. Nothing was downloaded.');
      const name =
        values.projectName
          .replace(/[^a-z0-9_ -]/gi, '')
          .trim()
          .slice(0, 70) || 'material-plan';
      const url = URL.createObjectURL(pdf);
      try {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `${name}-buyer-material-plan.pdf`;
        anchor.click();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      setMessage('Buyer PDF downloaded. No order was submitted.');
    } catch (cause) {
      if (valid()) setError(cause instanceof Error ? cause.message : 'The buyer PDF could not be generated.');
    } finally {
      controller.current = null;
      if (isCurrent()) setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={open => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="app-dialog buyer-pdf-dialog">
        <DialogHeader>
          <DialogTitle>Buyer material plan PDF</DialogTitle>
          <DialogDescription>
            Choose one complete plan for each material group. The PDF includes sheet quantities, part quantities and
            actual nest layouts in inches.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit(generate)}>
          <fieldset disabled={busy} className="buyer-pdf-fields">
            <label>
              Job / project reference
              <input {...form.register('projectName')} maxLength={200} />
            </label>
            {form.formState.errors.projectName && <p role="alert">{form.formState.errors.projectName.message}</p>}
            <div className="buyer-pdf-groups">
              {groups.map((group, index) => (
                <label key={group.groupId}>
                  <span>{group.label}</span>
                  <select {...form.register(`choices.${index}`)} aria-label={`Plan for ${group.label}`}>
                    {group.choices.map(choice => (
                      <option key={choice.id} value={choice.id}>
                        {choice.label}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
            <label>
              Buyer notes (optional)
              <textarea {...form.register('notes')} maxLength={2000} rows={3} />
            </label>
            <p className="buyer-pdf-note">
              The reference and notes appear only in this report; they do not change the nest.
            </p>
          </fieldset>
          <p className="buyer-pdf-warning">
            Confirm material grade and specification before ordering. A material-family label alone does not establish
            grade.
          </p>
          {conditional && (
            <p className="buyer-pdf-warning">
              Conditional plan: the existing recorded piece has unverified availability and eligibility. Confirm it
              before reducing the purchase quantity. The PDF includes a full-sheet fallback.
            </p>
          )}
          {error && (
            <p role="alert" className="buyer-pdf-error">
              {error}
            </p>
          )}
          {message && <p role="status">{message}</p>}
          {busy && <p role="status">Validating selected nests and preparing the PDF…</p>}
          <div className="buyer-pdf-actions">
            <button className="secondary" type="button" onClick={onClose}>
              {busy ? 'Cancel export' : 'Close report'}
            </button>
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Preparing PDF…' : 'Download buyer PDF'}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
