# Admin email recipients

Open **Admin Settings → Email Recipients** (`/admin/settings?tab=emails`). Select an email type, check the people who should receive it, and click **Save recipients**. Each email type has its own list. Saving an empty list disables that email. Search matches names and email addresses. **Restore default recipients** removes a custom list.

For the Werco company (`slug=werco`), **Work order completed** and **Material received** default to these active accounts, matched by email without regard to case:

- Ashley Werthen — `awerthen@wercomfg.com`
- Jon Werthen — `jwerthen@wercomfg.com`
- Jon Werthen Jr. — `jmw@wercomfg.com`

Missing or inactive default accounts are reported on the screen; no role-based fallback broadens the list. Other companies retain their existing defaults. Other automated email types keep their role, department, or record-owner audience until an administrator saves an explicit list. Account security emails and manually sent documents retain their existing recipients. Comment notifications (**You were mentioned**, **New comment**) are not listed and cannot be given a list: their audience is the person a comment mentions or the people watching the work order, so the `PUT` refuses those two keys with **400**, and a list saved for **You were mentioned** before 2026-11-16 is ignored. See [NOTIFICATIONS.md → Admin email recipients](NOTIFICATIONS.md#admin-email-recipients).

An explicit list controls email independently of the users' roles and personal channel preferences. Selected users receive email for their own actions too. In-app and SMS audiences retain their existing rules, including actor exclusion. Explicit lists do not create additional digest copies. Pending notification deliveries and digest content are checked against the current list before SMTP submission; removed recipients are recorded as `suppressed`, without raising a delivery-failure alert. An email already submitted cannot be recalled.

**Reaches now (2026-11-23).** Each email type shows who it actually reaches under the *saved* settings: the selected list when one is saved (or a shipped default), otherwise the automatic audience — the catalog's roles or departments, active users only, with each person's own email choice applied. Only deliverable addresses are listed. When the event also reaches people taken from each record (its creator, or someone the code names, such as a visitor's host), the box adds "plus the record's own people". An email type that reaches no one and depends on no record is flagged **Reaches nobody**, and a line at the top of the tab lists every such type. Unsaved edits are not previewed. Departments match by exact name, which is why Department is now a pick list (see [API.md → Users (Admin)](API.md#users-admin)).

## Storage and API

No schema migration is required. Lists use the existing tenant-scoped `quote_settings` table with keys `notification_email_recipients.<event_key>` and a JSON array of user IDs. The existing `(company_id, setting_key)` unique constraint supports the lookup. Missing settings use defaults; `[]` disables delivery; invalid stored JSON disables that email rather than reverting to a broader audience.

- `GET /api/v1/admin/settings/email-recipients` returns email types, selections, missing defaults, and the company's user directory. Each event also carries `audience_source` (`selected` | `automatic`), `reaches_user_ids`, `depends_on_record` and `reaches_nobody`; the `PUT` response has the same shape.
- `PUT /api/v1/admin/settings/email-recipients/{event_key}` accepts `{"user_ids": [1, 2]}`; `null` restores defaults.

Both endpoints require an administrator and use the active company. Updates validate active users with deliverable addresses, serialize saves using the company row, and record old/new user IDs in the settings audit log (the Admin page's history). Each save also writes a `notification_email_recipients` UPDATE row to the tamper-evident audit log (`GET /api/v1/audit/`), with the effective old/new `user_ids` (restoring the default records the audience it resolves to) and a `uses_default` flag that separates the shipped default audience from an explicit selection. A save that changes neither the audience nor `uses_default` writes no row to either log. The general overhead-setting endpoint cannot write these reserved keys. The self-service preference response also reflects the administrator's email selection.

Deploy the backend API, background worker, and frontend together. Werco's restricted defaults apply when the updated worker starts; the admin screen does not need to be opened or saved first. No test emails are sent by the verification suite.
