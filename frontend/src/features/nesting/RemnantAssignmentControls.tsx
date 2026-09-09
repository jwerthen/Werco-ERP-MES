import React, { useState } from 'react';
import type { RemnantPlan } from '../../types/remnantPlanning';
import type { QuoteProject } from './lib/quote-project';
import { quoteToFile } from './lib/quoting';
import RemnantPiecePicker from './RemnantPiecePicker';

export default function RemnantAssignmentControls({
  project,
  companyId,
  canPlan,
  disabled,
  ready,
  onSelect,
  onClear,
}: {
  project: QuoteProject;
  companyId?: number;
  canPlan: boolean;
  disabled: boolean;
  ready: boolean;
  onSelect: (plan: RemnantPlan) => void;
  onClear: () => void;
}) {
  const [pickerGroup, setPickerGroup] = useState<string | null>(null);
  const group = project.groups.find(item => item.id === (pickerGroup ?? project.activeGroupId));
  const plan = project.remnantPlan;
  if (!canPlan && !plan) return null;
  return (
    <section className="cad-source-panel remnant-planning-panel" aria-label="Recorded-piece planning assignment">
      <h3>Try one recorded piece</h3>
      <p>
        Explicitly compare reported material with your full-sheet baseline. Availability and eligibility are unverified;
        no piece is reserved or consumed.
      </p>
      {plan && (
        <>
          <p>
            <strong>{plan.snapshot.label}</strong> · observation {plan.snapshot.observationNumber} · assigned group{' '}
            {plan.groupId} · declared {plan.assignment.family}, {plan.assignment.requiredGrade},{' '}
            {plan.assignment.thicknessIn} in
          </p>
          <p role="status">
            {ready
              ? 'Source checked and assignment confirmed for these group inputs. Current physical availability is still unverified.'
              : 'Refresh the source and reaffirm this assignment before a new conditional calculation. Full-sheet comparisons remain available.'}
          </p>
          <p>
            Additional unavailable-zone clearance: {plan.zoneClearanceIn} in · assignment reason:{' '}
            {plan.assignment.reason}
          </p>
        </>
      )}
      <div className="team-draft-actions">
        {canPlan && companyId && (
          <button
            className="secondary"
            disabled={disabled || !project.groups.find(item => item.id === project.activeGroupId)?.quote.parts.length}
            onClick={() => setPickerGroup(project.activeGroupId)}
          >
            {plan ? 'Choose another recorded piece' : 'Choose recorded piece'}
          </button>
        )}
        {plan && canPlan && companyId && (
          <button className="secondary" disabled={disabled} onClick={() => setPickerGroup(plan.groupId)}>
            Refresh and reaffirm assignment
          </button>
        )}
        {plan && (
          <button
            className="secondary"
            disabled={disabled}
            onClick={() => {
              setPickerGroup(null);
              onClear();
            }}
          >
            Remove conditional piece
          </button>
        )}
      </div>
      {pickerGroup && group && companyId && canPlan && (
        <RemnantPiecePicker
          companyId={companyId}
          groupId={group.id}
          quote={JSON.parse(JSON.stringify(quoteToFile(group.quote)))}
          onSelect={next => {
            setPickerGroup(null);
            onSelect(next);
          }}
          onCancel={() => setPickerGroup(null)}
        />
      )}
    </section>
  );
}
