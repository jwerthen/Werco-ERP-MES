import { writeFileSync } from 'node:fs';

// Railway stamps its artifact in CI. Vercel's Git build must stamp its own exact
// source SHA before Vite copies public files into the immutable build output.
const sha = process.env.VERCEL_GIT_COMMIT_SHA;
if (!/^[a-f0-9]{40}$/.test(sha || '')) {
  throw new Error('Vercel build requires an exact VERCEL_GIT_COMMIT_SHA');
}
writeFileSync(new URL('../public/release.txt', import.meta.url), sha);
