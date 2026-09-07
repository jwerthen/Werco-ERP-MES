# Coordinated public website and API releases

The public ERP domain is hosted on Vercel. Railway also hosts a frontend, but verifying
that secondary frontend did not prevent Vercel's Git integration from publishing the
new website before the matching API. The previous release exposed this mismatch for
roughly 28 minutes while main-branch CI and the API deployment completed.

## Release flow

1. Git still builds Vercel previews and production artifacts. Production auto-assignment
   is disabled on the existing `werco-erp-mes` project, so the public domain stays on
   the previous artifact while tests and backend deployment run.
2. Both production workflow paths reject superseded commits before any service mutation and use the existing `production-deploy` concurrency
   group. The full path retains all required checks and exact Railway release gates.
3. Before a frontend-only release, the pipeline checks the actual live API SHA. It must
   either equal the frontend SHA or be an ancestor whose complete `backend/` contents
   match. Comparing only the latest push would miss a preceding failed backend release.
4. The promotion script verifies the Vercel project, staging setting, production branch,
   ready state and exact source SHA. Preview artifacts cannot be promoted through it.
5. It checks live API health/compatibility and the latest main commit again immediately
   before promotion. Superseded commits yield to the newer release.
6. After assigning the domain, it verifies `https://wercomfg.app/release.txt` by exact
   body and rechecks API compatibility. SPA fallback HTML does not count as a receipt.

Vercel's build command now stamps its own immutable source SHA into `public/release.txt`
before Vite builds. The receipt has `Cache-Control: no-store`. Railway retains its
existing CI stamp. Preview builds also receive accurate receipts.

## Setup prerequisites

GitHub production environment:

- Secret `VERCEL_TOKEN`: an ERP-project-scoped Vercel credential.
- Variable `VERCEL_PROJECT_ID`: `prj_WFmAR9zpc77NkqbdnUgniBkgCcbV`.
- Variable `VERCEL_TEAM_ID`: `team_LbjHUurSea8NEtY6euWHQ368`.
- Variable `PUBLIC_APP_URL`: `https://wercomfg.app`.
- Existing repository variable `PRODUCTION_API_URL` remains the Railway API origin.

On that Vercel project, set `autoAssignCustomDomains=false` after the credential is
stored and the promotion path is validated. This changes future assignment, not the
currently served website. No DNS or existing-domain migration is needed.

The CLI OAuth session can inspect the project but cannot mint an automation credential.
Its attempted project-token creation returned 403 without creating a token. Account
browser sign-in is required for that setup step. No credential values are included
in source, logs, screenshots or this document.

## Failures and recovery

An incompatible/unhealthy API, failed/still-building website, wrong project/branch,
missing credential or enabled auto-assignment stops promotion. A superseded main
commit is skipped. The public website stays on the prior artifact until a valid
promotion is submitted. If the promotion was submitted but its public receipt does
not verify, the job reports that uncertainty explicitly; inspect aliases instead of
claiming deployment success.

Rerunning the production job checks an already-current artifact rather than blindly
republishing it. Normal rollback should move API and UI together through the same
compatibility checks; never promote an older UI against changed backend contents
without verifying compatibility. The pipeline's compatibility policy assumes API
changes preserve the old UI while a staged release is waiting, as the incremental
schema changes in this round do.

Focused tests exercise compatible frontend-only ancestry, failed prior API releases,
preview/wrong-project/wrong-SHA rejection, old API refusal, staging misconfiguration,
failed build refusal, superseded commits, check-only mode and promotion/health order.
The final live deployment must demonstrate that the custom domain remains on the old
receipt while the API deploys, then advances to the matching website SHA.

Primary references: [staged production builds](https://vercel.com/docs/deployments/promoting-a-deployment),
[project configuration API](https://vercel.com/docs/rest-api/projects/update-an-existing-project),
[promotion API](https://vercel.com/docs/rest-api/projects/point-production-traffic-to-a-given-deployment).
