// Read-only validation of frontend permission replacement against canonical backend defaults.
// Run from repository root: node docs/codebase-audit-2026-09-13/evidence/frontend-permissions-repro.cjs
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = process.cwd();
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
const source = fs.readFileSync(path.join(root, 'frontend/src/utils/permissions.ts'), 'utf8');
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const exportsObject = {};
vm.runInNewContext(output, { exports: exportsObject, require: () => { throw new Error('Unexpected runtime import'); } });
const python = fs.readFileSync(path.join(root, 'backend/app/models/role_permission.py'), 'utf8');
const adminBlock = python.split('UserRole.ADMIN: [')[1].split('],')[0];
const defaults = Array.from(adminBlock.matchAll(/'([^']+)'/g), match => match[1]);
const permissions = ['parts:renumber', 'inventory:combine', 'process_sheets:author', 'process_sheets:release', 'visitor_logs:view'];
console.log('BEFORE_BACKEND_DEFAULTS', JSON.stringify(Object.fromEntries(permissions.map(p => [p, exportsObject.hasPermission('admin', p)]))));
exportsObject.setCustomPermissions({ admin: defaults });
console.log('AFTER_BACKEND_DEFAULTS', JSON.stringify(Object.fromEntries(permissions.map(p => [p, exportsObject.hasPermission('admin', p)]))));
