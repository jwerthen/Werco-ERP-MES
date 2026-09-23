import type { HankIntakeEvidence, HankIntakeFile } from '../../types/hankIntake';

export function hankEvidenceLabel(file: HankIntakeFile, evidence: HankIntakeEvidence): string {
  if (evidence.locator) return evidence.locator;
  const format = file.source_format || file.analysis?.source_format || (/\.pdf$/i.test(file.filename) ? 'pdf' : 'docx');
  if (format === 'pdf') return `Page ${evidence.page}`;
  return (
    file.source_labels?.[evidence.page - 1] ||
    file.analysis?.source_labels?.[evidence.page - 1] ||
    `Source section ${evidence.page}`
  );
}
