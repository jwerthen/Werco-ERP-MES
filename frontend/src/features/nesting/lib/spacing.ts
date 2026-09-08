import { inToMm } from './units';

/** Estimator-chosen starting allowances, not machine cutting parameters. */
export const AUTO_QUOTING_SPACING_POLICY_INCHES = Object.freeze({
  minimumGap: 0.125,
  gapThicknessMultiplier: 1,
  minimumMargin: 0.375,
  marginThicknessMultiplier: 2,
});

/**
 * Return contour-to-contour gap and sheet edge margin in internal millimeters.
 * The same initial policy applies to carbon steel, stainless steel, and aluminum;
 * each material/thickness group can override it with the shop's own allowances.
 *
 * Lantek documents configurable material/thickness separation tables; AMADA
 * distinguishes geometry spacing from beam width and lead-in/out allowances:
 * https://www.lantek.com/ca/cad-cam-nesting-software-oxycut-plasma-laser-waterjet
 * https://amada.com/amadasoftware/ap100us_help_file/Sheet_Wizard.htm
 * Neither source prescribes the numeric policy above. It is a conservative
 * quoting starting point, not a universal standard or an Ermaksan 6 kW recipe.
 */
export function autoQuotingSpacing(thicknessMm: number): { gap: number; margin: number } {
  const policy = AUTO_QUOTING_SPACING_POLICY_INCHES;
  if (!Number.isFinite(thicknessMm) || thicknessMm <= 0) {
    throw new Error('Enter a positive, finite material thickness.');
  }
  const gap = Math.max(inToMm(policy.minimumGap), thicknessMm * policy.gapThicknessMultiplier);
  const margin = Math.max(inToMm(policy.minimumMargin), thicknessMm * policy.marginThicknessMultiplier);
  if (!Number.isFinite(gap) || !Number.isFinite(margin)) {
    throw new Error('Material thickness is too large for quoting allowances.');
  }
  return { gap, margin };
}
