// Usage: node scripts/measure-route-bundles.mjs BUILD_DIR [BUILD_DIR...]
// Build with `vite build --manifest`. Counts emitted JS required by the entry
// plus the route's static import graph, excluding unopened dynamic features.
import { readFileSync } from 'node:fs';
import { resolve, basename } from 'node:path';
import { gzipSync } from 'node:zlib';

const routes = ['src/pages/WorkOrders.tsx', 'src/pages/Scheduling.tsx'];
const reports = process.argv.slice(2).map(input => {
  const dir = resolve(input);
  const manifest = JSON.parse(readFileSync(resolve(dir, '.vite/manifest.json'), 'utf8'));
  const closure = (root, visited = new Set()) => {
    if (visited.has(root)) return visited;
    if (!manifest[root]) throw new Error(`Manifest entry not found: ${root}`);
    visited.add(root);
    for (const dependency of manifest[root].imports || []) closure(dependency, visited);
    return visited;
  };
  const summarize = keys => {
    const files = [...new Set([...keys].map(key => manifest[key].file))].filter(file => file.endsWith('.js')).sort();
    return {
      files,
      minified_bytes: files.reduce((sum, file) => sum + readFileSync(resolve(dir, file)).length, 0),
      gzip_bytes: files.reduce((sum, file) => sum + gzipSync(readFileSync(resolve(dir, file))).length, 0),
    };
  };
  const entry = closure('index.html');
  return {
    build: basename(dir),
    entry: summarize(entry),
    routes: Object.fromEntries(routes.map(route => {
      const routeGraph = closure(route);
      return [route, {
        initial_navigation: summarize(new Set([...entry, ...routeGraph])),
        incremental_after_app: summarize(new Set([...routeGraph].filter(key => !entry.has(key)))),
        dynamic_features: (manifest[route].dynamicImports || []).map(key => manifest[key]?.file || key),
      }];
    })),
  };
});
process.stdout.write(`${JSON.stringify(reports, null, 2)}\n`);
