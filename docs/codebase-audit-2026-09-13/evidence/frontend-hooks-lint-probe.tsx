// Intentional invalid hook usage to verify the configured lint gate detects it.
import { useState } from 'react';
export function AuditProbe({ enabled }: { enabled: boolean }) {
  if (enabled) {
    const [value] = useState(0);
    return <div>{value}</div>;
  }
  return null;
}
