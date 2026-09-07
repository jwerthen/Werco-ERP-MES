# Werco ERP/MES — current UX foundations audit

> Persistent source review. Source citations are pinned to [audited commit `0a383b8c63926dd02e2a37c5153b43a3b095631b`](https://github.com/jwerthen/Werco-ERP-MES/tree/0a383b8c63926dd02e2a37c5153b43a3b095631b). The document is stored in the working checkout, but its evidence refers only to this pinned current revision, originally extracted at `/tmp/werco-ux-audit-current`. This is source-review evidence, not a claim of visual coverage.

**Authoritative source:** origin/main commit `0a383b8` (2026-09-06), extracted to `/tmp/werco-ux-audit-current`. All relative file references below resolve against THAT directory, not the older workspace checkout. This report completely supersedes the initial checkout analysis. The workspace checkout was 317 commits behind, and many initial defects were already fixed. Read-only review; root agent owns browser captures. No product files modified.

## Outcome

Current Werco already has substantial UI modernization: shared Modal/Button/FormField/DataTable primitives, accessible toast announcements, mobile cards, dirty-form refresh protection, route-aware page titles, scroll restoration, a real Central-time shift indicator, and greatly improved Dispatch Board controls. The remaining opportunities are completeness and operational trust: accurate freshness, consistent role-based discovery, preservation of in-progress work during SPA navigation, reliable modal pickers, account recovery, search interaction edge cases, and notification-state reconciliation.

## Prioritized current findings

### C1 — P1: A visible account-recovery button still does nothing

**Evidence:** [frontend/src/pages/Login.tsx:331-336](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Login.tsx#L331-L336) renders “Forgot password?” as a `type="button"` with no onClick, href, or form action.

**Impact:** A locked-out user has no recovery path from the sign-in surface.

**Recommendation:** Provide actual self-service recovery, or an actionable administrator recovery/contact flow until reset exists. Do not present an inert control.

**Acceptance:** Mouse and keyboard activation result in a real reset/contact flow, clearly state next steps, and allow return to sign-in without losing the account identifier.

**Already fixed / do not report:** Password-mode 401 errors are now exempted from hard redirects ([frontend/src/services/api.ts:285-292,330](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L285-L292)). Password mode now accepts email or employee ID with correct autocomplete. The password-visibility button still has no accessible name, but this is a smaller issue than the dead recovery path.

### C2 — P1: SelectField popups can render behind the modal that contains them

**Evidence:** Shared Modal backdrop/container uses `z-[60]` ([frontend/src/components/ui/Modal.tsx:181-199](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Modal.tsx#L181-L199)). SelectField portals its popup to document.body at `z-50` ([frontend/src/components/ui/SelectField.tsx:184-188,245](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/SelectField.tsx#L184-L188)). A real caller is Visitor Log’s Add visit form: [frontend/src/components/visitor/VisitorManualEntryModal.tsx:118](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/visitor/VisitorManualEntryModal.tsx#L118) opens Modal and [:167-178](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/visitor/VisitorManualEntryModal.tsx#L167-L178) renders its Purpose SelectField. Scrap reason controls also use SelectField ([frontend/src/components/quality/ScrapReasonFields.tsx:99-107](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/quality/ScrapReasonFields.tsx#L99-L107)). Newer ComboBox explicitly addresses the same issue with z-[70] ([frontend/src/components/ui/ComboBox.tsx:25-26](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/ComboBox.tsx#L25-L26)).

**Impact:** A user can open a required picker and see no usable option list, preventing completion of an otherwise simple dialog. The list may be visually obscured and inaccessible to pointer use under the backdrop.

**Recommendation:** Consolidate SelectField and ComboBox onto the more complete picker contract, or share an overlay layer/portal system. Make nested overlays participate in the same focus and Escape stack; a popup alone should close before the whole form.

**Acceptance:** In Add visit, open Purpose, select each option, cancel the picker, and continue with the form by pointer and keyboard. Repeat with scrap reason in completion dialogs and at small viewport heights. Options render above the dialog and stay in its accessible interaction scope. **Root browser reproduction requested; source stacking proof is high confidence.**

### C3 — P1: Some production “healthy” states are inferred from failed requests

**Evidence:** Dashboard auxiliary requests still map failures to `{open_ncrs:0}`, calibration `[]`, low-stock `[]`, and capacity null ([frontend/src/pages/Dashboard.tsx:204-229](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Dashboard.tsx#L204-L229)); the values suppress operational alerts and populate counts. The cached API fetch treats an error-based stale fallback as `fromCache:true,changed:false` ([frontend/src/services/api.ts:435-438](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L435-L438)), indistinguishable from a successful 304 ([:414-416](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L414-L416)). Setup now correctly shows an error banner ([frontend/src/pages/SetupWizard.tsx:128-133](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/SetupWizard.tsx#L128-L133)), but still replaces health with all-missing defaults ([:61-64](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/SetupWizard.tsx#L61-L64)) AND renders “No blocking master-data issues found. Your data is ready for production” when issues are empty ([:227-234](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/SetupWizard.tsx#L227-L234)).

**Impact:** Missing quality/stock information resembles zero issues. Setup can simultaneously warn that live data failed and affirm production readiness.

**Recommendation:** Give widgets distinct loading/data/error/stale states; retain last good values with timestamps; show unavailable rather than zero when unknown. Suppress readiness/success assertions while health is unavailable. Expose stale cache fallback separately from healthy conditional refresh.

**Acceptance:** Fail each request independently: no fabricated zero count, no readiness assertion, no loss of unaffected widgets, and an actionable retry. A displayed value has a truthful freshness/error state.

### C4 — P1: “SYNC OK” and “LIVE” are static even though actual shift is now correct

**Evidence:** [frontend/src/components/Layout.tsx:487-490](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L487-L490) calls useWebSocket but discards returned connection status. [:699-701](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L699-L701) renders LIVE unconditionally; [:883-886](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L883-L886) renders SYNC OK literally. Login similarly says SYSTEM LIVE ([frontend/src/pages/Login.tsx:116-121](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Login.tsx#L116-L121)).

**Impact:** A disconnected workstation still looks synchronized and operational. This is especially misleading while users make production decisions from dashboard/dispatch information.

**Recommendation:** Bind data synchronization to actual connection/data freshness, distinguishing reconnecting, offline, stale and successful synchronization. Do not conflate an open WebSocket with confirmed fresh data.

**Acceptance:** Disconnect/reconnect backend and observe each relevant state; show a reliable last successful sync timestamp. **Do not report SHIFT A hardcoding:** current `HudShift` is dynamic and Central-time aware ([Layout.tsx:445-461](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L445-L461)).

### C5 — P2: Role-aware navigation is partially implemented, but many disallowed destinations remain discoverable

**Evidence:** The current shell supports `NavItem.permission`, but gates only entries that explicitly define it ([frontend/src/components/Layout.tsx:584-599](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L584-L599)). Most legacy entries omit permission. For example, Dispatch Board is visible as an ungated item ([:127-129](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L127-L129)) while its route requires `work_orders:edit` ([frontend/src/App.tsx:156-159](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/App.tsx#L156-L159)); setup/import/users/calibration also retain ungated nav entries. Only operators receive a broad streamlined filter ([Layout.tsx:601-617](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L601-L617)). GlobalSearch’s setup/import/create command arrays remain unfiltered ([frontend/src/components/GlobalSearch.tsx:80-139,269-274](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L80-L139)). BottomNav gives all nonoperator roles identical defaults ([frontend/src/components/ui/BottomNav.tsx:25-50,71-75](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/BottomNav.tsx#L25-L50)).

**Impact:** Quality/shipping/viewer users and custom-role users see actions that lead to Access Denied. The underlying authorization is present, but discovery still feels unreliable.

**Recommendation:** Use shared route/action metadata to derive discovery and access requirements. Add explicit permissions to all legacy entries, filtering empty groups. Where a locked feature should stay discoverable, explain the access requirement before navigation and supply a concrete request-access path.

**Acceptance:** Test all built-in roles plus a custom permission override: every offered shortcut/action opens an allowed destination. Denied deep links have a useful next step. Keep backend authorization intact.

### C6 — P2: Dirty forms are protected on refresh/cancel, but SPA navigation still discards edits

**Evidence:** The new [frontend/src/hooks/useUnsavedChanges.ts:18-25](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/hooks/useUnsavedChanges.ts#L18-L25) explicitly documents that it does not intercept client-side route changes. It only covers beforeunload and explicit `confirmDiscard()` calls ([:41-65](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/hooks/useUnsavedChanges.ts#L41-L65)). PartEdit uses it ([frontend/src/pages/PartEdit.tsx:87-92](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/PartEdit.tsx#L87-L92)), as do WorkOrderNew and numerous other forms, while sidebar/breadcrumb Links remain ordinary SPA navigation.

**Impact:** A user carefully filling a long work order can click a sidebar link or browser Back and lose the form despite seeing protection on Cancel/refresh. The inconsistent boundary is hard to predict.

**Recommendation:** Add a supported router-level leave guard (or an equivalent central navigation policy), plus a visible saved/unsaved state. Add scoped drafts to long workflows where justified; preserve clear discard/save/stay choices.

**Acceptance:** Edit WorkOrderNew/PartEdit, then use sidebar, Back, breadcrumb and refresh: all discard paths require a deliberate decision or offer restoration. Successful saves clear dirty state, failures preserve edits. **Do not report dirty protection wholly absent; 20 production modules now import the hook.**

### C7 — P2: Authenticated deep links still lose their destination through sign-in

**Evidence:** PrivateRoute redirects to `/login` without storing location ([frontend/src/App.tsx:257-258](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/App.tsx#L257-L258)). Login computes only a role-based default landing path and navigates there ([frontend/src/pages/Login.tsx:69-78](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Login.tsx#L69-L78)). API session-expiry redirects drop location at [frontend/src/services/api.ts:348-350](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L348-L350). AuthContext includes `reason=idle` in an idle redirect ([frontend/src/context/AuthContext.tsx:51-57](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L51-L57)), but Login only reads kiosk query state.

**Impact:** An emailed notification/work-order link opened while signed out lands on the role’s default page, making the user search for the original task. Reauthentication during work loses context.

**Recommendation:** Carry a safe internal return path including query parameters through sign-in, validate authorization before returning, and explain idle/session-expiry reasons.

**Acceptance:** Open a signed-out deep link with query parameters, sign in, and arrive at the same allowed record. Preserve kiosk-specific flow. Denied return routes fall back with an explanation.

**Already improved:** Browser/page titles and detail-route titles now come from a shared source ([Layout.tsx:535-544](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L535-L544)), and scroll restoration exists ([:492-495](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L492-L495)). Sidebar active matching is still exact-path only ([:255-269](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L255-L269)), so parent highlighting on a detail page remains a smaller orientation opportunity.

### C8 — P2: Search has remaining race, error, and keyboard-model gaps despite better buttons/labels

**Evidence:** Search-open effect resets query/results whenever `recentItems.length` changes ([frontend/src/components/GlobalSearch.tsx:159-172](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L159-L172)). A delayed initial recents response can clear text typed immediately after opening. Search failure sets empty results ([:209-213](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L209-L213)), and UI says No results ([:415-423](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L415-L423)). Arrow/Enter handler uses recentItems on blank query ([:244-267](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L244-L267)) while the visible quick actions are a separate array ([:393-412](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L393-L412)). The query/results list lacks active-descendant/combobox relationship and active-result scroll-into-view. Stale results remain during the debounce interval ([:222-229,275](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/GlobalSearch.tsx#L222-L229)).

**Impact:** Typed input can disappear, the selected result may belong to a prior query, service failure looks like record absence, and the advertised arrows/Enter do not select visible empty-state quick actions from the search input.

**Recommendation:** Reset only on the closed-to-open transition; separate recent fetching; invalidate/cancel obsolete search work; provide explicit unavailable/retry UI; build a single navigable collection for recents/actions/results and announce active selection/result counts.

**Acceptance:** Delay recents while typing: preserve query. Return A after typing B: only B results are selectable. API failure is distinct from valid zero results. With zero recents, arrows/Enter activate each quick action. Long lists scroll active selection into view.

**Already fixed / do not report:** Input and clear button have accessible names; result rows and quick actions are now real tabbable buttons. This is a shortcut/combobox completeness issue, not total keyboard inaccessibility.

### C9 — P2: Accessibility adoption has remaining shell/session/dialog-label gaps

**Evidence:** Mobile drawer is always mounted and hidden with translate only ([frontend/src/components/Layout.tsx:663-689](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L663-L689)), without inert/focus lifecycle. Its open button has no accessible name/expanded state ([:797-803](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L797-L803)). SessionWarningModal remains styled plain divs without dialog semantics or focus management ([frontend/src/components/SessionWarningModal.tsx:30-55](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/SessionWarningModal.tsx#L30-L55)). Shared Modal is now correctly focus-trapped/restores focus ([frontend/src/components/ui/Modal.tsx:111-166](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Modal.tsx#L111-L166)) and supports `ariaLabelledBy` ([:193-199](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Modal.tsx#L193-L199)), but ConfirmDialog does not pass it or give its title an ID ([frontend/src/components/ui/ConfirmDialog.tsx:67-79](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/ConfirmDialog.tsx#L67-L79)); InputDialog similarly omits a modal accessible name ([frontend/src/components/ui/InputDialog.tsx:77-84](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/InputDialog.tsx#L77-L84)). Shared Tabs still omit selected-tab/panel relationships ([frontend/src/components/ui/Tabs.tsx:18-43](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Tabs.tsx#L18-L43)).

**Impact:** Keyboard users can tab into offscreen navigation; a session warning may not receive focus or be announced as a blocking dialog; screen readers hear generic unnamed confirmations. Selected tab is not programmatically identified.

**Recommendation:** Migrate remaining overlays onto the current shared primitives, wire names automatically in confirmation/input dialogs, and extend shell/tab contracts to include state and focus behavior.

**Acceptance:** Hidden mobile nav cannot receive focus; opening/closing drawer handles focus predictably; session warning is announced and operable; all modal dialogs have a computed accessible name; selected tabs and panels expose relationships. **Do not claim ConfirmDialog/KeyboardShortcutsModal still lack focus trapping; that was fixed.**

### C10 — P2: Legacy dark blue text still weakens key identifier/selected-tab contrast

**Evidence:** Current PartsNew part IDs use `text-werco-navy-600` ([frontend/src/pages/PartsNew.tsx:819,944](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/PartsNew.tsx#L819)), Materials IDs too ([frontend/src/pages/Materials.tsx:393](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Materials.tsx#L393)), and shared selected tabs use it ([frontend/src/components/ui/Tabs.tsx:29](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Tabs.tsx#L29)). Tailwind still defines #1B4D9C at [frontend/tailwind.config.js:24](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/tailwind.config.js#L24). Canvas/panel remain #0d1117/#141b26 ([frontend/src/index.css:50-61](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/index.css#L50-L61)). Declared sRGB token ratios are **2.33:1 on canvas, 2.13:1 on panel**.

**Impact:** Record identifiers and selected tabs are less readable than surrounding body text, particularly on variable shop-floor monitors.

**Recommendation:** Use a tested dark-surface link/active-text token instead of the brand fill color; verify essential small labels and metadata across both office and kiosk token scopes.

**Acceptance:** Browser-computed color pairs for identifiers, active tabs and essential labels meet the selected accessibility target (normal essential text at least 4.5:1); evaluate each actual surface. These ratios are source-color calculations, not complete rendered conformance results. The header’s SYNC/SHIFT labels were already changed from faint to mute; do not report that old issue.

### C11 — P2: Error/warning toasts still disappear after four seconds, including partial-success instructions

**Evidence:** Current Toast correctly adds role/live-region/name semantics ([frontend/src/components/ui/Toast.tsx:71-87](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Toast.tsx#L71-L87)) and distinguishes warning from error/success ([:7-16](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Toast.tsx#L7-L16)), but dismisses every type at four seconds ([:38-47](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/ui/Toast.tsx#L38-L47)). Comments explicitly describe warnings about incomplete duplication/material ties that users must act on.

**Impact:** A planner can miss the explanation that a job was created but an important tie did not carry over. Long multiline failures may vanish before being read.

**Recommendation:** Persist actionable error/warning messages until dismissed or resolved, optionally keeping a notification history or inline task banner. Use brief transient success feedback for uncomplicated confirmation.

**Acceptance:** Trigger a multiline failure and a partial-success warning: each stays available long enough to act on and has a clear next step; success remains lightweight; assistive announcements remain intact.

**Already fixed:** There are now zero alert()/confirm() source matches in production page modules. Feedback is widely standardized; do not report the stale 145-alert inventory.

### C12 — P2: Dashboard initial display still waits on all auxiliary widgets; background failures replace useful content

**Evidence:** Dashboard awaits five parallel requests as one Promise.all before committing results ([frontend/src/pages/Dashboard.tsx:212-229](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Dashboard.tsx#L212-L229)); shared Axios has no default timeout ([frontend/src/services/api.ts:229-236](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/services/api.ts#L229-L236)). Polling is every 30 seconds ([Dashboard.tsx:304-310](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Dashboard.tsx#L304-L310)). Failed background load sets a page error ([:279-280](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Dashboard.tsx#L279-L280)), and an early return replaces the dashboard with ErrorState ([:420-426](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Dashboard.tsx#L420-L426)). ErrorState now DOES offer retry. All lazy pages still use SkeletonDashboard ([frontend/src/App.tsx:93-97](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/App.tsx#L93-L97)).

**Impact:** A slow calibration/capacity request blocks core production data, and a temporary refresh failure hides already useful information. Forms briefly load as dashboard-shaped skeletons.

**Recommendation:** Resolve widgets independently, preserve last-good data on background failure with freshness labels, define appropriate request budgets, and choose route-shaped loading skeletons.

**Acceptance:** Delay an auxiliary endpoint: core dashboard renders when core data arrives. Fail a background load: useful data remains visible with retryable stale state. Measure real task/load timings before setting performance targets. No runtime latency/bundle measurements were performed here.

### C13 — P2: Dispatch Board has good local mutation UX but no automatic reconciliation with other stations

**Evidence:** Current DispatchBoard `load()` fetches the board and only resets staleNotice on successful fetch ([frontend/src/pages/DispatchBoard.tsx:324-349](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/DispatchBoard.tsx#L324-L349)). Its effect loads once. The source contains no interval, visibility refresh, or useWebSocket subscription; updates occur via manual Refresh or the local mutation flows. Header only shows stale warning when a requested refresh failed ([:1182-1191](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/DispatchBoard.tsx#L1182-L1191)), and has no last-fetched timestamp. Global shell nevertheless says SYNC OK.

**Impact:** A manager can leave the board open while kiosk operators start/finish work, or another manager reorders it, and see a silently aging view until manually refreshed.

**Recommendation:** Subscribe to relevant production/dispatch changes or add guarded background refresh and page-focus reconciliation; avoid reordering beneath an active drag/edit. Show last successful reconciliation and an explicit “updates available” mechanism if live refresh is deferred.

**Acceptance:** Open the board in two sessions; change order/start/complete externally. The other board updates safely or announces pending changes with a timestamp, preserving focus and any active interaction. Stale/error distinction remains clear.

**Strengths:** Current board already prioritizes machines with work and deactivated anomalies, has explicit horizontal scroll controls and capped height, shows material/changeover context, offers Move up/down + machine select for keyboard equivalence, announces resulting positions, and differentiates optimistic reorders from server-gated cross-machine moves. Do not recommend these as missing features.

### C14 — P2: Notification inbox and bell can disagree after a mark-read action; bulk scope is ambiguous

**Evidence:** Bell maintains independent unreadCount/items state and polls every 60 seconds ([frontend/src/components/NotificationBell.tsx:27-30,35-52](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/NotificationBell.tsx#L27-L30)). Full inbox marks rows read in its own state ([frontend/src/pages/Notifications.tsx:128-140](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Notifications.tsx#L128-L140)) without notifying/reconciling the bell. The inbox’s Mark all read calls the global endpoint ([:148-152](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Notifications.tsx#L148-L152)) but disables based only on unread rows on the current page ([:158,256](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Notifications.tsx#L158)). In Unread-only view, markRead flips a row to read without removing/refetching it ([:128-140](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Notifications.tsx#L128-L140)). Inbox request flow has no request-sequence/abort protection against rapid filter changes ([:89-112](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/pages/Notifications.tsx#L89-L112)).

**Impact:** Reading notifications can leave an obsolete header badge; a filtered page may retain rows that no longer match. “Mark all read” may be unavailable even when other pages have unread notifications, while activating it clears beyond the visible filter scope.

**Recommendation:** Use one notification query/state cache (or explicit invalidation) for bell and inbox; reconcile affected filtered pages, preserve pagination metadata, and label bulk scope as global or filtered. Cancel/ignore stale filter requests.

**Acceptance:** Read a notification in either surface: badge and both lists reconcile immediately. Unread-only rows leave appropriately. Mark all read’s scope is explicit and enabled using the correct count. Rapid filter changes never show an older result set under the newest filter.

### C15 — P2 verification item: Session warning can reset the timers that generated it

**Evidence:** [frontend/src/context/AuthContext.tsx:67-69](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L67-L69) sets sessionWarning true at 14 minutes. The activity effect depends on sessionWarning ([:106](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L106)), cleans up timers ([:100-104](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L100-L104)), and calls resetIdleTimer when re-running ([:97-98](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L97-L98)). resetIdleTimer clears warning ([:63-64](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L63-L64)) and schedules new timers ([:67-74](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/AuthContext.tsx#L67-L74)). This logic is unchanged by the newer kiosk token-listener additions.

**Inference:** The warning can flash/close itself and postpone expiry rather than remain as a stable one-minute countdown. This is a source logic trace, not a 15-minute browser reproduction.

**Recommendation/acceptance:** Decouple listener/timer lifetime from warning-state changes and derive warning from one expiry deadline. Verify the real AuthProvider with fake timers: after 14 minutes warning remains stable, no action reaches expiry, explicit Stay Logged In renews once, rerenders do not extend it. Maintain explicit kiosk policy.

### C16 — P3: Help discoverability and completion scope do not fully fit shared/mobile workstations

**Evidence:** Help & Tours remains desktop-only ([frontend/src/components/Layout.tsx:911-914](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L911-L914)). Auto-start-attempt is newly per-user ([:518-533](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/Layout.tsx#L518-L533)), but completed tours still use one shared localStorage key ([frontend/src/context/TourContext.tsx:52-60,78-83](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/context/TourContext.tsx#L52-L60)), so another user’s completed state can suppress the “once per user” tour. AdaptivePromptPanel still uses unscoped dismiss/visit keys ([frontend/src/components/AdaptivePromptPanel.tsx:6-19](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/AdaptivePromptPanel.tsx#L6-L19)) and “Open blockers” navigates to its same pathname, including `/work-orders/new` ([:24-30](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/components/AdaptivePromptPanel.tsx#L24-L30)).

**Recommendation/acceptance:** Make help reachable on mobile; scope completion and dismissals per user/company; route hints to the actual action/panel and exclude inappropriate create routes. User A’s completed guide must not imply user B completed it.

## Current shared-component adoption — verified, tests excluded

Static import-site counts; not runtime counts. These demonstrate real improvement over the old checkout.

| Component/hook | Production import sites |
|---|---:|
| Modal | 59 |
| Button | 49 |
| LoadingButton | 34 |
| FormField | 65 |
| DataTable | 32 |
| MobileDataCard | 23 |
| MobileDataList | 2 |
| EmptyState | 33 |
| ErrorState | 50 |
| useToast | 75 |
| StatusBadge | 34 |
| useUnsavedChanges | 20 |
| Breadcrumbs | 9 |
| Tabs | 3 |
| ComboBox | 3 |
| FormErrorBoundary | 0 |
| AsyncBoundary | 0 |
| useFormErrorMapping / useAsyncValidation | 0 each |

Production page-module source matches: **0 alert() and 0 confirm()**. The dirty hook intentionally still uses native confirm for explicit discard; do not imply all native dialogs are absent throughout source. Unused FormErrorBoundary/AsyncBoundary/helper modules alone are not a user-visible defect; use the established modern primitives where they already solve the problem.

## Current route inventory / scope

**89 Route declarations: 88 explicit paths + wildcard. 74 non-test page TSX modules.** Route declarations include legacy redirects, print/kiosk/public display variants and analytics aliases, not 88 distinct business workflows.

- Access/display: `/login`, `/register`, `/register-company`, `/unauthorized`, `/wallboard`, `/tv`, `/tv/:code`, `/visitor-signin`, `/kiosk`, wildcard.
- Home/action/setup: `/`, `/action-inbox`, `/notifications`, `/settings`, `/setup`, `/import-center`.
- Production: `/work-orders`, `/work-orders/new`, `/work-orders/:id`, `/shop-floor`, `/shop-floor/operations`, `/scheduling`, `/dispatch`, `/downtime`, `/maintenance`, `/oee`, `/work-centers`.
- Engineering: `/parts`, `/parts/:id`, `/parts/:id/edit`, `/bom`, `/bom/uom-mismatches`, `/routing`, `/process-sheets`, `/engineering-changes`.
- Warehouse/procurement: `/warehouse`, `/materials`, `/purchasing`, `/po-upload`, `/mrp`, `/tool-management`; legacy inventory/receiving/shipping routes and `/purchasing/:poId`, `/shipping/:shipmentId` compatibility redirects.
- Sales: `/nest`, `/rfq-packages/new`, `/quotes`, `/quotes/:quoteId` compatibility, `/quote-calculator`, `/estimate-workbench`, `/estimate-workbench/:estimateId`, `/shop-data`, `/customers`.
- Quality: `/quality`, `/quality/:legacyTab/:legacyId` compatibility, `/calibration`, `/calibration/:equipmentId` compatibility, `/traceability`, `/spc`, `/customer-complaints`, `/qms-standards`, `/supplier-scorecards`, `/certifications`.
- Insights: `/analytics` and production/quality/inventory/forecasting/costs/flow/reports suffixes; `/reports`, `/job-costing`.
- Admin: `/users`, `/admin/settings`, `/audit-log`, `/custom-fields`, `/documents`, `/platform`, `/visitor-log`.
- Print: traveler, purchase-order, packing-slip, shipping-label by ID; `/print/badges`.

## Strengths / retain

- Shared primitives are now heavily adopted, with real tests and consistent feedback/error normalization.
- Modal has nested-modal stack handling, keyboard focus trapping and restoration. Toast has live regions, named dismiss controls, and warning semantics.
- FormField systematically associates labels/help/errors. ComboBox has a more complete ARIA, keyboard and overlay-layer design than older SelectField.
- DataTable provides shared sorting, pagination, CSV, mobile variants and error/empty/loading regions.
- Dirty-form refresh/close handling exists and should be extended to route transitions.
- Route-aware page/tab titles and scroll restoration exist; shift and clock use shop-local Central time.
- Dispatch Board has unusually thoughtful keyboard alternatives, explicit operational semantics and focus preservation; extend freshness while preserving those behaviors.
- NotificationBell preserves last-known count on poll failure and rolls back failed optimistic mark-read changes.
- Lazy loading, ETag conditional requests and shared WebSocket connection reuse are implemented. Do not infer multiple physical sockets from multiple hook consumers.
- Reduced-motion/high-contrast support exists ([frontend/src/styles/accessibility.css:18-44](https://github.com/jwerthen/Werco-ERP-MES/blob/0a383b8c63926dd02e2a37c5153b43a3b095631b/frontend/src/styles/accessibility.css#L18-L44)), with skip link and focusable main.

## Limits / next verification

This subagent used source inspection only. Root is collecting rendered evidence. No actual login attempts, writes, screenshots, production fault injection, screen-reader session, bundle build, Web Vitals measurement or long-running timeout test was performed here. Parent should connect source-backed findings to screenshots and distinguish browser-confirmed defects from source-inferred risks. The modal picker stack, session timer loop, search race, stale dashboard states and multi-session dispatch behavior deserve targeted reproductions. No accessibility certification or performance timing claim is implied.


## Separate live verification supplement

The coordinating reviewer subsequently reproduced **C2** in the live application. In Add visit, opening Purpose produced an expanded listbox whose options existed in the DOM but were hidden behind the modal, with only a small bottom portion visible. The reviewer changed no value and submitted nothing. This observation strengthens the modal-layering finding for this specific caller; it does not establish that every SelectField caller fails. See [visitor Purpose picker screenshot](../screenshots/30-visitor-purpose-picker.jpg).
