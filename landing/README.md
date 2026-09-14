# Manufacturing ERP Landing Page

The shipped marketing site is the static `index.html`. Vite serves and builds that
entry. The optional React implementation under `src/` is not imported by the HTML,
so editing those components does not change the published site.

## Development

Use Node.js 22 and the committed lockfile:

```bash
cd landing
npm ci
npm run dev
```

Open **http://localhost:3001**. This serves the same static HTML entry used by the
production build; it does not switch to the React implementation. You can also open
`index.html` directly in a browser, though its Tailwind CDN script and remote assets
still require network access.

## Checks and build

```bash
npm run type-check
npm run build
npm run audit:ci
```

- `type-check` validates the optional React/TypeScript source independently of the
  shipped entry. Keeping it type-correct does not mount it or assert that it has the
  same content as the static page.
- `build` produces the static site's deployment output in `dist/`.
- `audit:ci` checks the locked npm tree and exits nonzero for high/critical advisories.

The main CI pipeline runs **Landing Checks** (`npm ci`, type-check, build) before
Docker builds. The separate nightly/manual dependency-audit workflow runs the landing
audit. The CDN script in `index.html` is outside the npm lockfile and npm audit's scope.

## Project structure

| Path | Purpose |
|---|---|
| `index.html` | Shipped page, including layout, copy, styling configuration, and scripts |
| `src/` | Optional React implementation; separately type-checked, currently unmounted |
| `vite.config.ts` | Development server and `dist/` build configuration |
| `package-lock.json` | Reproducible npm dependency tree |

## Customization and deployment

Edit `index.html` to change the current site's copy, branding, pricing, sections,
and interactions. Review marketing claims and sample testimonials before publication.
Adopting the React source would require an explicit HTML-entry change and browser
validation; it is not part of the current development or build command.

For the existing Vite deployment, run `npm run build` and publish `dist/` to the
configured static host. This repository's landing site deploys independently from
the ERP application. Running local checks does not publish either site.
