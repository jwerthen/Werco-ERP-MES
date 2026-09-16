import React, { useState } from 'react';
import { CheckCircle2, CircleAlert } from 'lucide-react';
import { fabricationQuoteApi } from './api';
import { AddButton, Card, Empty, Section, TextField, Toggle } from './components';
import { money, readableError, scenarioPlan } from './model';
import type { CalculationResult, QuotePlan } from './types';
import { newId } from './types';

const costLabels: Record<string, string> = { material_cost: 'Allocated material', purchased_parts_cost: 'Purchased parts', labor_cost: 'Labor', machine_cost: 'Machine occupancy', consumables_cost: 'Consumables', outside_cost: 'Outside services', hardware_consumed_cost: 'Hardware consumed' };
export function CostSummary({ result, currency, current }: { result: CalculationResult | null; currency: string; current: boolean }) {
  const totals = result?.totals || {};
  const blockers = result?.issues.filter(i => i.severity === 'blocking').length ?? 0;
  return <aside className="fq-cost-summary"><div className="fq-eyebrow">QUOTE SUMMARY</div><div className="fq-summary-price">{result ? money(totals.selling_price, currency) : 'Not calculated'}</div><p className="fq-hint">{!result ? 'Add inputs, then calculate.' : !current ? 'Inputs changed. Calculate again.' : blockers ? `${blockers} blocking ${blockers === 1 ? 'item' : 'items'} to resolve.` : 'All required costing inputs reviewed.'}</p>
    <dl>{Object.entries(costLabels).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{result ? money(totals[key], currency) : '—'}</dd></div>)}<div className="fq-total"><dt>Total cost</dt><dd>{result ? money(totals.total_cost, currency) : '—'}</dd></div></dl>
    {result && totals.total_cost == null && <p className="fq-hint">Known cost so far: {money(totals.known_cost, currency)}. This excludes unresolved costs.</p>}
    <div className="fq-summary-note"><span>Hardware procurement cash</span><strong>{result ? money(totals.hardware_procurement_cash, currency) : '—'}</strong><small>Purchase cash may exceed the hardware consumed by this job.</small></div>
    {result && <div className={`fq-readiness ${current && result.can_approve ? 'fq-readiness-ok' : ''}`}>{current && result.can_approve ? <CheckCircle2 size={18} /> : <CircleAlert size={18} />}<span>{!current ? 'Calculation out of date' : result.can_approve ? 'Calculation checks complete' : 'Review still required'}</span></div>}
  </aside>;
}
export function ReviewPanel({ plan, onChange, result, current, editable, quoteId }: { plan: QuotePlan; onChange: (plan: QuotePlan) => void; result: CalculationResult | null; current: boolean; editable: boolean; quoteId?: number }) {
  const [scenarioText, setScenarioText] = useState('');
  const [scenarios, setScenarios] = useState<{ quantity: string; result: CalculationResult }[]>([]);
  const [scenarioInput, setScenarioInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const compare = async () => {
    if (busy) return; setError('');
    try {
      const quantities = scenarioText.split(',').map(q => q.trim()).filter(Boolean);
      if (!quantities.length || quantities.length > 5) throw new Error('Enter one to five quantities, separated by commas.');
      const plans = quantities.map(q => scenarioPlan(plan, q));
      setBusy(true); const results = await Promise.all(plans.map(p => fabricationQuoteApi.calculate(p, quoteId)));
      setScenarios(quantities.map((quantity, i) => ({ quantity, result: results[i] }))); setScenarioInput(JSON.stringify(plan));
    } catch (e) { setError(readableError(e)); } finally { setBusy(false); }
  };
  return <>
    <Section title="Calculation review" description="Every total traces back to quantities, a process recipe and reviewed evidence.">
      {!result && <Empty>Calculate the plan to see costs, quantity rollups and review items.</Empty>}
      {result && <><div className="fq-review-meta"><span className={`fq-pill ${current ? 'fq-pill-green' : 'fq-pill-amber'}`}>{current ? 'Current inputs' : 'Stale result'}</span><span>Engine {result.engine_version}</span><span>As of {String(result.as_of || '—')}</span></div>{result.issues.length ? <ul className="fq-issues">{result.issues.map((issue, i) => <li key={`${issue.path}-${issue.code}-${i}`} className={issue.severity === 'blocking' ? 'fq-issue-blocking' : ''}><CircleAlert size={17} /><div><strong>{issue.message}</strong><span>{issue.severity === 'blocking' ? 'Blocking' : 'Warning'} · {issue.path || 'Plan'} · {issue.code}</span></div></li>)}</ul> : <div className="fq-success"><CheckCircle2 size={19} />All calculation checks passed. Review the inputs and release note before approval.</div>}
        {result.demand && result.demand.length > 0 && <div className="fq-table-wrap"><table><caption>Exploded demand</caption><thead><tr><th>Part</th><th>Make / buy</th><th>Total quantity</th></tr></thead><tbody>{result.demand.map(row => <tr key={row.part_id}><td>{plan.parts.find(p => p.id === row.part_id)?.name || row.part_id}</td><td>{row.make_or_buy}</td><td>{row.quantity}</td></tr>)}</tbody></table></div>}
        {(result.operation_lines?.length ?? 0) > 0 && <div className="fq-table-wrap"><table><caption>Operation costing</caption><thead><tr><th>Operation</th><th>Setups</th><th>Runs</th><th>Labor hours</th><th>Machine hours</th><th>Cost</th></tr></thead><tbody>{result.operation_lines?.map((line, i) => <tr key={i}><td>{plan.operations.find(o => o.id === line.id)?.name || String(line.id)}</td><td>{String(line.setup_count)}</td><td>{String(line.run_multiplier)}</td><td>{line.labor_seconds == null ? 'Unknown' : (Number(line.labor_seconds) / 3600).toFixed(3)}</td><td>{line.machine_seconds == null ? 'Unknown' : (Number(line.machine_seconds) / 3600).toFixed(3)}</td><td>{money(line.cost, plan.currency)}</td></tr>)}</tbody></table></div>}
        {(result.hardware_lines?.length ?? 0) > 0 && <div className="fq-table-wrap"><table><caption>Hardware consumption & purchasing</caption><thead><tr><th>Exact item</th><th>Required</th><th>Buy each</th><th>Excess each</th><th>Consumed cost</th><th>Purchase cash</th></tr></thead><tbody>{result.hardware_lines?.map((line, i) => <tr key={i}><td>{String(line.manufacturer)} · {String(line.mpn)}</td><td>{String(line.required_quantity)}</td><td>{String(line.purchase_quantity)}</td><td>{String(line.excess_inventory_quantity)}</td><td>{money(line.consumed_cost, plan.currency)}</td><td>{money(line.procurement_cash, plan.currency)}</td></tr>)}</tbody></table></div>}
        <details className="fq-details"><summary>Calculation manifest & full breakdown</summary><pre className="fq-json">{JSON.stringify(result, null, 2)}</pre></details>
      </>}
    </Section>
    <Section title="Quantity scenarios" description="Compare one root assembly at different quantities. These exploratory results do not change the saved quote."><div className="fq-row"><TextField label="Scenario quantities" value={scenarioText} onChange={setScenarioText} placeholder="e.g. 1, 10, 50" /><button type="button" className="fq-btn fq-btn-secondary" disabled={busy || plan.roots.length !== 1 || !scenarioText.trim()} onClick={() => void compare()}>{busy ? 'Calculating…' : 'Compare quantities'}</button></div>{plan.roots.length !== 1 && <p className="fq-hint">Specify exactly one top-level demand to compare quantity breaks.</p>}{error && <div role="alert" className="fq-alert">{error}</div>}{scenarios.length > 0 && <>{scenarioInput !== JSON.stringify(plan) && <p className="fq-alert">Plan inputs changed. Recalculate these scenarios.</p>}<div className="fq-table-wrap"><table><thead><tr><th>Quantity</th><th>Lot cost</th><th>Lot price</th><th>Unit price</th><th>Review</th></tr></thead><tbody>{scenarios.map((scenario, i) => <tr key={i}><td>{scenario.quantity}</td><td>{money(scenario.result.totals.total_cost, plan.currency)}</td><td>{money(scenario.result.totals.selling_price, plan.currency)}</td><td>{scenario.result.totals.selling_price == null ? 'Unresolved' : money(Number(scenario.result.totals.selling_price) / Number(scenario.quantity), plan.currency)}</td><td>{scenario.result.can_approve ? 'Checks complete' : `${scenario.result.issues.filter(issue => issue.severity === 'blocking').length} blockers`}</td></tr>)}</tbody></table></div></>}</Section>
    <fieldset disabled={!editable}><Section title="Assumptions & scope" description="Record decisions that affect the estimate. Unreviewed assumptions block approval." action={<AddButton onClick={() => onChange({ ...plan, assumptions: [...plan.assumptions, { id: newId('assumption'), description: '', reviewed: false, source: null }] })}>Add assumption</AddButton>}>
      {!plan.assumptions.length && <Empty>No additional assumptions recorded.</Empty>}{plan.assumptions.map((a, i) => <Card key={a.id} title={`Assumption ${i + 1}`} onRemove={() => onChange({ ...plan, assumptions: plan.assumptions.filter((_, j) => i !== j) })}><TextField label="Scope / assumption" value={a.description} onChange={description => onChange({ ...plan, assumptions: plan.assumptions.map((row, j) => j === i ? { ...a, description, reviewed: false } : row) })} /><TextField label="Assumption source" value={a.source} onChange={source => onChange({ ...plan, assumptions: plan.assumptions.map((row, j) => j === i ? { ...a, source: source || null, reviewed: false } : row) })} /><Toggle label="Assumption reviewed" checked={a.reviewed} onChange={reviewed => onChange({ ...plan, assumptions: plan.assumptions.map((row, j) => j === i ? { ...a, reviewed } : row) })} /></Card>)}
    </Section></fieldset>
  </>;
}
