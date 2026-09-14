# Admin email recipients

Open **Admin Settings → Email Recipients** (`/admin/settings?tab=emails`). Select an email type, check the people who should receive it, and click **Save recipients**. Each email type has its own list. Saving an empty list disables that email. Search matches names and email addresses. **Restore default recipients** removes a custom list.

For the Werco company (`slug=werco`), **Work order completed** and **Material received** default to these active accounts, matched by email without regard to case:

- Ashley Werthen — `awerthen@wercomfg.com`
- Jon Werthen — `jwerthen@wercomfg.com`
- Jon Werthen Jr. — `jmw@wercomfg.com`

Missing or inactive default accounts are reported on the screen; no role-based fallback broadens the list. Other companies retain their existing defaults. Other automated email types keep their role, department, or record-owner audience until an administrator saves an explicit list. Account security emails and manually sent documents retain their existing recipients.

An explicit list controls email independently of the users' roles and personal channel preferences. Selected users receive email for their own actions too. In-app and SMS audiences retain their existing rules, including actor exclusion. Explicit lists do not create additional digest copies. Pending notification deliveries and digest content are checked against the current list before SMTP submission; removed recipients are recorded as `suppressed`, without raising a delivery-failure alert. An email already submitted cannot be recalled.

## Storage and API

No schema migration is required. Lists use the existing tenant-scoped `quote_settings` table with keys `notification_email_recipients.<event_key>` and a JSON array of user IDs. The existing `(company_id, setting_key)` unique constraint supports the lookup. Missing settings use defaults; `[]` disables delivery; invalid stored JSON disables that email rather than reverting to a broader audience.

- `GET /api/v1/admin/settings/email-recipients` returns email types, selections, missing defaults, and the company's user directory.
- `PUT /api/v1/admin/settings/email-recipients/{event_key}` accepts `{"user_ids": [1, 2]}`; `null` restores defaults.

Both endpoints require an administrator and use the active company. Updates validate active users with deliverable addresses, serialize saves using the company row, and record old/new user IDs in the settings audit log. The general overhead-setting endpoint cannot write these reserved keys. The self-service preference response also reflects the administrator's email selection.

Deploy the backend API, background worker, and frontend together. Werco's restricted defaults apply when the updated worker starts; the admin screen does not need to be opened or saved first. No test emails are sent by the verification suite.
