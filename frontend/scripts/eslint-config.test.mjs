import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ESLint } from 'eslint';

// Exercise the discovered flat config, not an override that could pass while the
// application's actual lint gate silently stops loading these rules.
const eslint = new ESLint({ cwd: fileURLToPath(new URL('../', import.meta.url)) });

async function messages(source, filePath) {
  const [result] = await eslint.lintText(source, { filePath });
  return result.messages;
}

const conditionalHook = `
  import { useState } from 'react';
  export function ConditionalProbe({ enabled }: { enabled: boolean }) {
    if (enabled) {
      const [value] = useState(0);
      return <span>{value}</span>;
    }
    return null;
  }
`;

for (const filePath of ['src/pages/ConditionalProbe.tsx', 'src/components/ui/ConditionalProbe.tsx']) {
  test(`conditional Hooks are rejected in ${filePath}`, async () => {
    const findings = await messages(conditionalHook, filePath);
    assert.ok(findings.some(({ ruleId, severity }) => ruleId === 'react-hooks/rules-of-hooks' && severity === 2));
  });
}

test('unconditional Hooks with complete dependencies are accepted', async () => {
  const findings = await messages(`
    import { useEffect, useState } from 'react';
    export function ValidProbe({ label }: { label: string }) {
      const [value] = useState(0);
      useEffect(() => { document.title = label; }, [label]);
      return <span>{value}</span>;
    }
  `, 'src/components/ui/ValidProbe.tsx');
  assert.deepEqual(findings, []);
});

test('shared UI primitives reject missing effect dependencies', async () => {
  const findings = await messages(`
    import { useEffect } from 'react';
    export function StaleEffectProbe({ label }: { label: string }) {
      useEffect(() => { document.title = label; }, []);
      return <span>{label}</span>;
    }
  `, 'src/components/ui/StaleEffectProbe.tsx');
  assert.ok(findings.some(({ ruleId, severity }) => ruleId === 'react-hooks/exhaustive-deps' && severity === 2));
});
