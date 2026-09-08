import React, { useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import api from '../../services/api';
import { centralWallClockToUtcISO, formatCentralDateTime } from '../../utils/centralTime';
import type {
  NestingPolicyCommandBase,
  NestingPolicyPublishRequest,
  NestingPolicyPublication,
  NestingPolicyRevision,
  NestingPolicyRevisionRequest,
  NestingPolicyState,
  NestingPolicyReceipt,
} from '../../types/nestingPolicy';
import { validateSpacingContent, type SpacingPolicyContent } from './lib/spacing-policy';
import { canonicalJSON, sha256 } from './lib/provenance';
import { nestingApiMessage } from './useNestingCatalog';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import SpacingPolicyEditor from './SpacingPolicyEditor';

type Revision = NestingPolicyRevision & { content: SpacingPolicyContent };
type Command = { policyId: number | null } & (
  | { kind: 'draft'; body: NestingPolicyRevisionRequest; revisionNumber: number }
  | { kind: 'publish'; body: NestingPolicyPublishRequest; target: NestingPolicyRevision }
  | { kind: 'withdraw'; body: NestingPolicyCommandBase; target: NestingPolicyPublication }
);
const reviewSchema = z.object({
  reason: z.string().trim().min(1, 'Enter a reason for this decision.').max(1000),
  effectiveDate: z.string(),
});

function checkCompany(value: { company_id: number }, companyId: number) {
  if (value.company_id !== companyId) throw new Error('Policy history belongs to a different company.');
}
async function checkReceipt(result: NestingPolicyReceipt, command: Command) {
  const invalid = () => {
    throw new Error('The policy receipt does not match this decision. Retry the same request.');
  };
  const row = result.revision,
    publication = result.publication;
  checkCompany(row, command.body.expected_company_id);
  if (publication) checkCompany(publication, command.body.expected_company_id);
  if (
    result.schema_version !== 1 ||
    !Number.isSafeInteger(result.event_id) ||
    result.event_id < 1 ||
    result.policy_version !== command.body.expected_version + 1 ||
    !Number.isSafeInteger(row.id) ||
    row.id < 1 ||
    !Number.isSafeInteger(row.policy_id) ||
    row.policy_id < 1 ||
    (command.policyId !== null && row.policy_id !== command.policyId)
  )
    invalid();
  if (command.kind === 'draft') {
    if (
      publication !== null ||
      row.revision_number !== command.revisionNumber ||
      row.name !== command.body.content.name ||
      row.content_sha256 !== (await sha256(canonicalJSON(command.body.content)))
    )
      invalid();
    return;
  }
  const target = command.target;
  if (
    row.id !== (command.kind === 'publish' ? command.target.id : command.target.revision_id) ||
    row.revision_number !== target.revision_number ||
    row.content_sha256 !== target.content_sha256 ||
    !publication ||
    publication.policy_id !== row.policy_id ||
    publication.revision_id !== row.id ||
    publication.revision_number !== row.revision_number ||
    publication.content_sha256 !== row.content_sha256
  )
    invalid();
  if (!publication) return;
  if (command.kind === 'publish') {
    if (
      publication.id !== result.event_id ||
      publication.policy_version !== result.policy_version ||
      (command.body.effective_at !== null &&
        Date.parse(publication.effective_at) !== Date.parse(command.body.effective_at))
    )
      invalid();
  } else if (
    publication.id !== command.target.id ||
    publication.policy_version !== command.target.policy_version ||
    publication.withdrawal?.id !== result.event_id
  )
    invalid();
}

type ManagerProps = { companyId: number; canManage: boolean; onChanged: () => void };
export default function SpacingPolicyManager(props: ManagerProps) {
  // A company switch discards every old target and aborts its reads and writes.
  return <CompanySpacingPolicyManager key={props.companyId} {...props} />;
}
function CompanySpacingPolicyManager({ companyId, canManage, onChanged }: ManagerProps) {
  const [open, setOpen] = useState(false),
    [state, setState] = useState<NestingPolicyState | null>(null),
    [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false);
  const [error, setError] = useState(''),
    [message, setMessage] = useState('');
  const [revision, setRevision] = useState<Revision | null>(null),
    [editing, setEditing] = useState(false);
  const [decision, setDecision] = useState<'approve' | NestingPolicyPublication | null>(null);
  const [pending, setPending] = useState<Command | null>(null);
  const read = useRef<AbortController | null>(null),
    action = useRef<AbortController | null>(null),
    live = useRef(true),
    busyRef = useRef(false);
  const review = useForm<z.infer<typeof reviewSchema>>({
    resolver: zodResolver(reviewSchema),
    defaultValues: { reason: '', effectiveDate: '' },
  });
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      read.current?.abort();
      action.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    read.current?.abort();
    read.current = controller;
    setLoading(true);
    setError('');
    setRevision(null);
    setDecision(null);
    void api
      .getNestingSpacingPolicies(page, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.schema_version !== 1 || !Array.isArray(result.revisions) || !Array.isArray(result.publications))
          throw new Error('Invalid policy history.');
        if (result.policy) checkCompany(result.policy, companyId);
        if (result.current_publication) checkCompany(result.current_publication, companyId);
        [...result.revisions, ...result.publications].forEach(row => checkCompany(row, companyId));
        setState(result);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(nestingApiMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [open, page, refresh, companyId]);

  async function inspect(row: NestingPolicyRevision) {
    if (busyRef.current) return;
    const controller = new AbortController();
    read.current?.abort();
    read.current = controller;
    setLoading(true);
    setError('');
    setRevision(null);
    setDecision(null);
    try {
      const result = await api.getNestingSpacingRevision(row.revision_number, controller.signal);
      if (controller.signal.aborted) return;
      checkCompany(result, companyId);
      if (
        result.id !== row.id ||
        result.policy_id !== row.policy_id ||
        result.content_sha256 !== row.content_sha256 ||
        result.revision_number !== row.revision_number
      )
        throw new Error('The returned revision does not match the selected policy.');
      validateSpacingContent(result.content);
      if (result.name !== result.content.name)
        throw new Error('The returned policy name does not match its content. Refresh policy history.');
      if ((await sha256(canonicalJSON(result.content))) !== row.content_sha256)
        throw new Error('The returned policy content does not match its recorded hash. Refresh policy history.');
      if (!live.current || controller.signal.aborted) return;
      setRevision(result);
      setEditing(false);
      setDecision(null);
    } catch (cause) {
      if (!controller.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  function base(reason: string): NestingPolicyCommandBase {
    if (!state) throw new Error('Load current policy history first.');
    return {
      expected_company_id: companyId,
      expected_version: state.policy?.version ?? 0,
      request_key: crypto.randomUUID(),
      reason,
    };
  }
  async function execute(command: Command) {
    if (!canManage || busyRef.current || command.body.expected_company_id !== companyId) return;
    busyRef.current = true;
    setBusy(true);
    setPending(command);
    setError('');
    setMessage('');
    const controller = new AbortController();
    action.current = controller;
    try {
      const result =
        command.kind === 'draft'
          ? await api.createNestingSpacingRevision(command.body, controller.signal)
          : command.kind === 'publish'
            ? await api.publishNestingSpacingPolicy(command.body, controller.signal)
            : await api.withdrawNestingSpacingPolicy(command.target.id, command.body, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      await checkReceipt(result, command);
      if (!live.current || controller.signal.aborted) return;
      setPending(null);
      setEditing(false);
      setRevision(null);
      setDecision(null);
      setPage(1);
      setRefresh(value => value + 1);
      setMessage(
        command.kind === 'draft'
          ? `Saved policy draft revision ${result.revision.revision_number}. It is not approved.`
          : command.kind === 'publish'
            ? 'Policy approval recorded. Estimates change only when an estimator applies it.'
            : 'Withdrawal recorded. Existing saved evidence remains unchanged.'
      );
      onChanged();
    } catch (cause) {
      if (!live.current || controller.signal.aborted) return;
      const status = (cause as { response?: { status?: number } })?.response?.status;
      if (status && status >= 400 && status < 500 && status !== 408) setPending(null);
      setError(nestingApiMessage(cause));
    } finally {
      busyRef.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }
  const disabled = busy || !!pending || loading;
  const current = state?.current_publication;
  const summaryDate = (value: string) => `${formatCentralDateTime(value)} Central`;
  return (
    <>
      <button
        className="secondary compact"
        onClick={() => {
          setOpen(true);
          setRefresh(value => value + 1);
        }}
      >
        Review spacing policies
      </button>
      <Dialog
        open={open}
        onOpenChange={value => {
          if (!busyRef.current) setOpen(value);
        }}
      >
        <DialogContent className="spacing-policy-dialog">
          <DialogHeader>
            <DialogTitle>Shop quoting spacing policies</DialogTitle>
            <DialogDescription>
              Inches · immutable revisions and approval history. Approval applies to quoting allowances only.
            </DialogDescription>
          </DialogHeader>
          {loading && <p role="status">Loading policy history…</p>}
          {error && (
            <p role="alert" className="team-draft-error">
              {error}
            </p>
          )}
          {message && <p role="status">{message}</p>}
          {pending && !busy && (
            <div role="status">
              <p>The command is unconfirmed. Retrying recovers the same request.</p>
              <button className="primary compact" onClick={() => void execute(pending)}>
                Retry previous command
              </button>
            </div>
          )}
          {editing ? (
            <SpacingPolicyEditor
              key={revision?.id ?? 'new'}
              initial={revision?.content ?? null}
              disabled={disabled}
              onSave={(content, reason) =>
                void execute({
                  kind: 'draft',
                  body: { ...base(reason), content },
                  policyId: state?.policy?.id ?? null,
                  revisionNumber: (state?.policy?.latest_revision_number ?? 0) + 1,
                })
              }
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              <div className="team-draft-actions">
                <button
                  className="secondary compact"
                  disabled={busy || loading}
                  onClick={() => setRefresh(value => value + 1)}
                >
                  Refresh policy history
                </button>
                {canManage && (
                  <button
                    className="primary compact"
                    disabled={disabled || !state}
                    onClick={() => {
                      setRevision(null);
                      setDecision(null);
                      setEditing(true);
                    }}
                  >
                    New policy draft
                  </button>
                )}
              </div>
              <p>
                {current?.status === 'current'
                  ? `Current approval: revision ${current.revision_number}, effective ${summaryDate(current.effective_at)}.`
                  : current?.status === 'withdrawn'
                    ? 'The latest effective approval is withdrawn. No older policy is automatically reused.'
                    : 'No current approved policy. Starting allowances remain unreviewed.'}
              </p>
              {!canManage && (
                <p className="helper inset-free">
                  You can review policies. Administration requires an Admin with nesting write access.
                </p>
              )}
              {revision && (
                <section className="policy-revision-review" aria-label="Policy revision details">
                  <h3>
                    {revision.name} · revision {revision.revision_number}
                  </h3>
                  <p className="helper inset-free">
                    Author #{revision.created_by} · {summaryDate(revision.created_at)}
                  </p>
                  <div className="policy-table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Material</th>
                          <th>Thickness band (in)</th>
                          <th>Gap (in)</th>
                          <th>Edge margin (in)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {revision.content.bands.map(band => (
                          <tr key={band.id}>
                            <td>{band.material}</td>
                            <td>
                              {band.thickness_min_in} ≤ t &lt; {band.thickness_max_in}
                            </td>
                            <td>
                              max({band.minimum_gap_in}, t × {band.gap_thickness_multiplier})
                            </td>
                            <td>
                              max({band.minimum_margin_in}, t × {band.margin_thickness_multiplier})
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {canManage && (
                    <div className="team-draft-actions">
                      <button className="secondary compact" disabled={disabled} onClick={() => setEditing(true)}>
                        Create revised draft
                      </button>
                      <button
                        className="primary compact"
                        disabled={disabled}
                        onClick={() => {
                          review.reset({ reason: '', effectiveDate: '' });
                          setDecision('approve');
                        }}
                      >
                        Review approval
                      </button>
                    </div>
                  )}
                </section>
              )}
              {decision && canManage && (
                <form
                  className="policy-decision"
                  onSubmit={review.handleSubmit(values => {
                    try {
                      if (decision === 'approve') {
                        if (!revision) throw new Error('Select a policy revision to approve.');
                        const effective = values.effectiveDate
                          ? centralWallClockToUtcISO(`${values.effectiveDate}T00:00`)
                          : null;
                        if (values.effectiveDate && (!effective || Date.parse(effective) <= Date.now()))
                          throw new Error('Choose a future effective date, or leave it empty for immediate approval.');
                        void execute({
                          kind: 'publish',
                          policyId: revision.policy_id,
                          target: revision,
                          body: {
                            ...base(values.reason),
                            revision_number: revision.revision_number,
                            content_sha256: revision.content_sha256,
                            effective_at: effective,
                          },
                        });
                      } else
                        void execute({
                          kind: 'withdraw',
                          target: decision,
                          policyId: decision.policy_id,
                          body: base(values.reason),
                        });
                    } catch (cause) {
                      setError(nestingApiMessage(cause));
                    }
                  })}
                >
                  <fieldset disabled={disabled}>
                    <legend>
                      {decision === 'approve'
                        ? `Approve revision ${revision?.revision_number}`
                        : `Withdraw approval for revision ${decision.revision_number}`}
                    </legend>
                    {decision === 'approve' ? (
                      <>
                        <p>
                          Review the exact bands above. Your identity and this reason will be recorded. This does not
                          approve a nest or material specification.
                        </p>
                        <label className="field-label">
                          Effective date (Central)
                          <input type="date" {...review.register('effectiveDate')} />
                        </label>
                        <p className="helper inset-free">
                          Leave empty to take effect on approval. A future date begins at midnight Central.
                        </p>
                      </>
                    ) : (
                      <p>
                        Withdrawal preserves history and prevents this approval from supplying new estimates when
                        effective. A withdrawn future approval blocks policy use from its scheduled date unless
                        replaced. No older approval is automatically restored.
                      </p>
                    )}
                    <label className="field-label">
                      Decision reason
                      <textarea rows={3} maxLength={1000} {...review.register('reason')} />
                    </label>
                    {review.formState.errors.reason && <p role="alert">{review.formState.errors.reason.message}</p>}
                    <div className="team-draft-actions">
                      <button className="primary" type="submit">
                        {decision === 'approve' ? 'Approve this policy revision' : 'Withdraw this approval'}
                      </button>
                      <button className="secondary" type="button" onClick={() => setDecision(null)}>
                        Cancel decision
                      </button>
                    </div>
                  </fieldset>
                </form>
              )}
              <h3>Policy revisions</h3>
              {state && !state.revisions.length && <p>No policy drafts have been created.</p>}
              {state?.revisions.map(row => (
                <article className="team-draft-item" key={row.id}>
                  <div>
                    <strong>
                      {row.name} · revision {row.revision_number}
                    </strong>
                    <p>
                      {summaryDate(row.created_at)} · author #{row.created_by}
                    </p>
                  </div>
                  <button className="secondary compact" disabled={busy || loading} onClick={() => void inspect(row)}>
                    Review revision {row.revision_number}
                  </button>
                </article>
              ))}
              <h3>Approval history</h3>
              {state && !state.publications.length && <p>No approved policies. Saving a draft does not approve it.</p>}
              {state?.publications.map(row => (
                <article className="team-draft-item" key={row.id}>
                  <div>
                    <strong>
                      Revision {row.revision_number} · {row.status}
                    </strong>
                    <p>
                      Effective {summaryDate(row.effective_at)} · approved by #{row.created_by}
                    </p>
                    <p>Reason: {row.reason}</p>
                    {row.withdrawal && (
                      <p>
                        Withdrawn {summaryDate(row.withdrawal.created_at)} by #{row.withdrawal.created_by}:{' '}
                        {row.withdrawal.reason}
                      </p>
                    )}
                  </div>
                  {canManage && !row.withdrawal && (
                    <button
                      className="secondary compact"
                      disabled={disabled}
                      onClick={() => {
                        review.reset({ reason: '', effectiveDate: '' });
                        setDecision(row);
                      }}
                    >
                      Review withdrawal
                    </button>
                  )}
                </article>
              ))}
              <div className="team-draft-actions">
                <button
                  className="secondary compact"
                  disabled={busy || loading || page <= 1}
                  onClick={() => setPage(value => value - 1)}
                >
                  Previous history page
                </button>
                <span>Page {page}</span>
                <button
                  className="secondary compact"
                  disabled={
                    busy ||
                    loading ||
                    !state ||
                    page * state.per_page >= Math.max(state.total_revisions, state.total_publications)
                  }
                  onClick={() => setPage(value => value + 1)}
                >
                  Next history page
                </button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
