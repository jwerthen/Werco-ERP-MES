#!/usr/bin/env python3
"""Generate the ADMIN GO-LIVE MASTER CHECKLIST PDF — the single exhaustive runbook
the admin works top-to-bottom to make sure everything for go-live is done.

Output: docs/onboarding/quick-reference/00-ADMIN-MASTER-CHECKLIST.pdf
Run:    backend/.venv311/bin/python docs/onboarding/generate_admin_master_checklist.py

Content verified against current origin/main (post PRs #113-#120) on 2026-07-13.
Regenerate after system changes — this script is the source, the PDF is a build artifact.
Reuses the design language of generate_onboarding_pdfs.py (Werco instrument-panel).
"""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick-reference")
os.makedirs(OUT, exist_ok=True)

NAVY = colors.HexColor("#1B4D9C")
RED = colors.HexColor("#C8352B")
INK = colors.HexColor("#16202E")
GRAY = colors.HexColor("#4B5563")
LIGHT = colors.HexColor("#8A94A3")
LINE = colors.HexColor("#D3D8DF")
PANEL = colors.HexColor("#EEF0F3")
AMBER = colors.HexColor("#8A5A00")
AMBER_BG = colors.HexColor("#FDF3DD")
RED_BG = colors.HexColor("#FAE7E5")
GREEN = colors.HexColor("#1E6B3A")
GREEN_BG = colors.HexColor("#E4F2E8")

MARGIN = 44
PAGE_W, PAGE_H = letter
CONTENT_W = PAGE_W - 2 * MARGIN

S = {
    "eyebrow": ParagraphStyle("eyebrow", fontName="Courier-Bold", fontSize=7.5, leading=10, textColor=LIGHT),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=22, leading=25, textColor=INK, spaceAfter=2),
    "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=9.5, leading=13, textColor=GRAY),
    "sec": ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=13, leading=15, textColor=INK),
    "secidx": ParagraphStyle("secidx", fontName="Courier-Bold", fontSize=11, leading=15, textColor=RED),
    "owner": ParagraphStyle("owner", fontName="Courier-Bold", fontSize=7, leading=10, textColor=LIGHT, alignment=2),
    "item": ParagraphStyle("item", fontName="Helvetica", fontSize=9.2, leading=12.2, textColor=INK),
    "why": ParagraphStyle("why", fontName="Helvetica", fontSize=8, leading=10.5, textColor=GRAY),
    "lead": ParagraphStyle("lead", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor=GRAY),
    "callout": ParagraphStyle("callout", fontName="Helvetica", fontSize=9, leading=12, textColor=INK),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.4, leading=10.6, textColor=INK),
    "cellg": ParagraphStyle("cellg", fontName="Helvetica", fontSize=8.4, leading=10.6, textColor=GRAY),
    "th": ParagraphStyle("th", fontName="Courier-Bold", fontSize=7, leading=9, textColor=GRAY),
    "toc": ParagraphStyle("toc", fontName="Helvetica", fontSize=9, leading=13.5, textColor=INK),
    "tocn": ParagraphStyle("tocn", fontName="Courier-Bold", fontSize=9, leading=13.5, textColor=RED),
}


def _deco(canvas, doc):
    w, h = letter
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 6, w, 6, stroke=0, fill=1)
    canvas.setFont("Courier-Bold", 6.5)
    canvas.setFillColor(LIGHT)
    canvas.drawString(MARGIN, h - 22, "WERCO ERP-MES  \xb7  ADMIN GO-LIVE MASTER CHECKLIST  \xb7  WERCOMFG.APP")
    canvas.drawRightString(w - MARGIN, h - 22, "WERCO-OB-00")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, h - 28, w - MARGIN, h - 28)
    canvas.line(MARGIN, 40, w - MARGIN, 40)
    canvas.setFont("Courier", 6.5)
    canvas.setFillColor(LIGHT)
    canvas.drawString(MARGIN, 30, "VERIFIED AGAINST PRODUCTION MAIN  \xb7  2026-07-13  \xb7  REGEN: generate_admin_master_checklist.py")
    canvas.drawRightString(w - MARGIN, 30, "REV E \xb7 PAGE %d" % doc.page)
    canvas.restoreState()


def checkbox():
    t = Table([[""]], colWidths=[10], rowHeights=[10])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, GRAY), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def checklist(items):
    """items: list of (text, why|None)."""
    rows = []
    for text, why in items:
        cell = [Paragraph(text, S["item"])]
        if why:
            cell.append(Paragraph(why, S["why"]))
        rows.append([checkbox(), cell])
    t = Table(rows, colWidths=[18, CONTENT_W - 18])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (1, 0), (1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
    ]))
    return t


def callout(text, kind="red"):
    bar, bg = {"red": (RED, RED_BG), "amber": (AMBER, AMBER_BG), "navy": (NAVY, PANEL),
               "green": (GREEN, GREEN_BG)}[kind]
    p = Paragraph(text, S["callout"])
    t = Table([["", p]], colWidths=[5, CONTENT_W - 5])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), bar),
        ("BACKGROUND", (1, 0), (1, -1), bg),
        ("TOPPADDING", (1, 0), (1, -1), 7), ("BOTTOMPADDING", (1, 0), (1, -1), 7),
        ("LEFTPADDING", (1, 0), (1, -1), 9), ("RIGHTPADDING", (1, 0), (1, -1), 9),
    ]))
    return t


def section(story, idx, title, owner, lead=None):
    head = Table(
        [[Paragraph(idx, S["secidx"]), Paragraph(title, S["sec"]), Paragraph(owner, S["owner"])]],
        colWidths=[26, CONTENT_W - 26 - 120, 120],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(Spacer(1, 10))
    story.append(head)
    story.append(HRFlowable(width="100%", thickness=1.1, color=INK, spaceAfter=3, spaceBefore=1))
    if lead:
        story.append(Paragraph(lead, S["lead"]))
        story.append(Spacer(1, 3))


def subhead(story, text):
    story.append(Spacer(1, 5))
    story.append(Paragraph(text.upper(), ParagraphStyle("sh", fontName="Helvetica-Bold", fontSize=8.5,
                                                        leading=11, textColor=NAVY)))
    story.append(Spacer(1, 2))


def table(story, headers, rows, widths):
    data = [[Paragraph(h, S["th"]) for h in headers]]
    for r in rows:
        data.append([Paragraph(c, S["cell"] if j == 0 else S["cellg"]) for j, c in enumerate(r)])
    w = [CONTENT_W * f for f in widths]
    t = Table(data, colWidths=w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(t)


story = []

# ---- Masthead -------------------------------------------------------------
story.append(Paragraph("WERCO ERP-MES \xb7 AS9100D \xb7 ISO 9001 \xb7 CMMC L2 \xb7 GO-LIVE WEEK MON JUL 13, 2026", S["eyebrow"]))
story.append(Spacer(1, 4))
story.append(Paragraph("Admin Go-Live Master Checklist", S["title"]))
story.append(Paragraph(
    "The single runbook you work top-to-bottom to make sure everything for go-live is done. Every item was verified "
    "against production code on 2026-07-13. Check "
    "each box; nothing on the "
    "floor or in the office is truly ready until its section is green. Work sections 0–6 before any training session.",
    S["sub"]))
story.append(Spacer(1, 8))
story.append(HRFlowable(width="100%", thickness=1.6, color=NAVY, spaceAfter=8))

# ---- Contents -------------------------------------------------------------
subhead(story, "What this covers")
toc = [
    ("0", "What changed since the last plan (read first)"), ("1", "Platform &amp; environment readiness"),
    ("2", "Data cutover from Excel"), ("3", "Fail-closed master-data gates"),
    ("4", "Accounts, roles &amp; badges"), ("5", "Station terminals"),
    ("6", "CUI egress switches (3 decisions)"), ("7", "Per-role readiness check"),
    ("8", "Training schedule (Mon–Fri)"), ("9", "Support &amp; incident model"),
    ("10", "Go / no-go scoreboard"), ("11", "Known sharp edges"), ("12", "Week-2 punch list"),
]
trows = []
half = (len(toc) + 1) // 2
for i in range(half):
    left = toc[i]
    right = toc[i + half] if i + half < len(toc) else None
    row = [Paragraph(left[0], S["tocn"]), Paragraph(left[1], S["toc"])]
    if right:
        row += [Paragraph(right[0], S["tocn"]), Paragraph(right[1], S["toc"])]
    else:
        row += ["", ""]
    trows.append(row)
tt = Table(trows, colWidths=[22, CONTENT_W / 2 - 22, 22, CONTENT_W / 2 - 22])
tt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                        ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 2),
                        ("LEFTPADDING", (2, 0), (2, -1), 0), ("RIGHTPADDING", (2, 0), (2, -1), 2)]))
story.append(tt)

# ---- 0. What changed ------------------------------------------------------
section(story, "0", "What changed since the last plan", "READ FIRST",
        "Seven fixes merged and deployed 2026-07-12/13. These change what admins and managers can do — brief people "
        "before they hit a surprise.")
table(story, ["PR", "Change", "What it means for you"], [
    ["#114", "User management is now Admin-only", "Only Admins create/edit/deactivate users, reset passwords, import CSV, and print badges. Managers keep a READ-ONLY user list; Supervisors lose the Users page (get /unauthorized). Managers can no longer print badges."],
    ["#115 / #119", "Password strength enforced everywhere", "Creating/resetting/changing any password now needs 12+ chars, upper+lower+digit+special, no common words — weak values are refused (422). Operator badge accounts auto-generate a compliant password (exempt)."],
    ["#116", "Visitor staff back-entry added", "Admin/Manager can “Add visit” on the Visitor Log with the REAL past times after a tablet outage. Needs DB migration 064 applied."],
    ["#117", "Shop-floor back-entry toggle added", "Supervisors+ get a “Back-entry (offline catch-up)” toggle that tags labor so it's excluded from live metrics. Operators don't have it."],
    ["#113 / #118", "Doc corrections", "User-management IS now audited (older guides said it wasn't). Deploy runbook is DEPLOYMENT_RUNBOOK.md; access token is 15 min."],
], [0.11, 0.30, 0.59])
story.append(Spacer(1, 4))
story.append(callout(
    "<b>Two RBAC facts that will generate “it's broken” tickets:</b> a Manager still SEES the Users list but every "
    "write button is gone (read-only by design), and a Supervisor opening /users now gets /unauthorized. An Admin cannot "
    "change their <b>own</b> role, and platform_admin can't be assigned from the tenant at all — both reasons you need "
    "a second Admin.", "amber"))

# ---- 1. Platform readiness ------------------------------------------------
section(story, "1", "Platform &amp; environment readiness", "OWNER: ADMIN / DEVOPS",
        "Do these against the Railway prod services. A green health check does NOT mean these are done — several are "
        "invisible to the automated checks.")
checklist_1 = [
    ("Confirm <b>ALLOW_DB_RESET</b> is unset/false on the werco-api service.",
     "Footgun: with it true + the SECRET_KEY, POST /auth/reset-database TRUNCATES every table. It is NOT in ENVIRONMENT_VARIABLES.md and verify_launch does NOT test it — a green verifier does not disarm it."),
    ("Confirm <b>RATE_LIMIT_ENABLED=true</b> and <b>REDIS_URL</b> is set.",
     "Without Redis the limiter is per-worker memory, so with multiple replicas the effective limit multiplies and auth throttles aren't shared. verify_launch only WARNS on these — don't accept a warn."),
    ("Set <b>SENTRY_DSN</b>; confirm ENVIRONMENT=production, DEBUG=false, SECRET_KEY &amp; REFRESH_TOKEN_SECRET_KEY ≥ 32 chars, CORS_ORIGINS = real prod origin (no localhost).", None),
    ("Run the launch verifier and clear every <b>[FAIL]</b>: <font face='Courier'>railway run --service werco-api python -m scripts.verify_launch</font>.", None),
    ("Verify the <b>ARQ worker</b> service is deployed and running as a SEPARATE process from the API.",
     "No /health endpoint reports worker liveness. If it's dead: email, MRP runs, receiving-label auto-print, the carrier tracking-poll cron, AND the monthly audit-archive job all silently stop — the last is an AS9100D retention failure."),
    ("Point <b>AUDIT_ARCHIVE_DIR</b> at a durable, backed-up, worker-writable volume.",
     "The monthly job writes tamper-evident audit exports there; an ephemeral/missing volume is a compliance gap, not just ops."),
    ("Confirm the DB is at migration head <b>064</b> (<font face='Courier'>064_visitor_log_entered_by</font>): <font face='Courier'>railway run --service werco-api alembic upgrade head</font>.",
     "The visitor “Add visit” feature and its export column depend on 064. (Empty-DB bootstrap is create_all → stamp 058 → upgrade, NOT a bare upgrade head.)"),
    ("Check <b>/health</b> and <b>/health/ready</b> return 200; spot-check /health/detailed features shows rate_limiting=true, redis=true, sentry=true.", None),
    ("Put <b>/health/detailed</b> behind auth or a network/IP restriction.",
     "It is unauthenticated and leaks DB version, DB host, pool state, and environment — an avoidable CMMC finding."),
    ("Rehearse rollback once: redeploy the prior commit (Railway → Deployments → Redeploy), <font face='Courier'>alembic downgrade -1</font>, and a DB backup+restore; confirm /health/ready after.",
     "A code rollback does NOT roll back a schema migration — know both levers before an incident."),
    ("Declare the change-control posture for the week (merge windows / freeze).",
     "main auto-deploys to prod ~30–50 min after any merge, with only a health curl as the tripwire; E2E is non-blocking."),
]
story.append(checklist(checklist_1))

# ---- 2. Data cutover ------------------------------------------------------
section(story, "2", "Data cutover from Excel", "OWNER: ADMIN \xb7 EXCEL_MIGRATION_RUNBOOK.md",
        "The Import Center page is Admin-only. Excel freezes at cutover — no parallel run; paper travelers may mirror "
        "the system and get reconciled in, but the workbooks become a read-only archive.")
checklist(story) if False else None
story.append(checklist([
    ("Rehearse the full 11-step sequence <b>twice</b> as a dry run with the REAL exported files; keep a migration log (file, counts, fixes).",
     "Dry runs write nothing — safe in prod. Cutover day executes the log, not an improvisation. Never commit-rehearse in the production company."),
    ("Load order (validate → read preview → commit on every step): <b>1</b> Work Centers → <b>2</b> Users → <b>3</b> Customers → <b>4</b> Vendors → <b>5</b> Parts → <b>6</b> Materials → <b>7</b> BOMs → <b>8</b> Routings → <b>9</b> Inventory → <b>10</b> Open POs → <b>11</b> Open WOs.", None),
    ("After step 8, <b>RELEASE every imported routing</b> for parts that have open WOs.",
     "Imported routings land DRAFT; the open-WO import (step 11) refuses any part without a released routing. Budget real time — the runbook calls this the longest step."),
    ("Do a <b>physical inventory count</b> and key it via Warehouse → Inventory → Receive (captures lot numbers).",
     "There is no inventory bulk import; the Import Center inventory CSV is a counting worksheet only."),
    ("Freeze Excel in writing the night before; export fresh files from the frozen workbooks for the real import.", None),
    ("After step 11, walk the floor with a supervisor — every work-center queue matches reality and jobs sit at the right operation (wrong completed_through_seq is the usual culprit). Confirm Receiving lists the open POs.", None),
    ("Clean up the leftover prod TEST records — this is an <b>API/DB task, not a UI task</b>.",
     "Customer TES001 → backend DELETE /customers/{id} (no button exists). Part TEST-CLQA-001 → parts delete path. Lot TEST-LOT-CLQA-01 → no delete endpoint at all; can only be zeroed via POST /inventory/adjust. Assign a named owner with API/DB access."),
]))

# ---- 3. Master-data gates -------------------------------------------------
section(story, "3", "Fail-closed master-data gates", "OWNER: QUALITY + ENGINEERING",
        "These block the floor on day 1 if the data is dirty — the server refuses, with no kiosk override. Seed them "
        "before the first operator wave.")
story.append(checklist([
    ("Every gauge an operator will scan exists in Equipment, status ACTIVE, and has a <b>next_calibration_date on/after today</b>.",
     "Fail-closed: a gauge with NO due date is refused just like an expired one (409 GAUGE_OUT_OF_CAL). Seeding ACTIVE is not enough — it needs a valid future date."),
    ("Active <b>scrap reason codes</b> exist (Quality → Scrap), or you accept the legacy free-text grid.",
     "Scrap without a reason is refused (422) on production reports and clock-outs."),
    ("Every part with a week-1 WO has a <b>RELEASED routing</b>, and every attached <b>process-sheet family has a released revision</b>.",
     "A routing pointing at a sheet family with no released revision blocks WO creation (409 PROCESS_SHEET_UNAVAILABLE). “Imported”/“authored” does not mean “usable on the floor” — releasing is a separate step."),
    ("Decide the force-complete policy: who may use the audited WO-level steps-gate bypass, and when.",
     "It exists for legacy/paper-evidenced jobs; habitual use quietly erodes the AS9100D evidence posture (the steps_bypassed audit entries are the detection signal)."),
]))

# ---- 4. Accounts / roles / badges -----------------------------------------
section(story, "4", "Accounts, roles &amp; badges", "OWNER: ADMIN",
        "User creation, role assignment, and badge printing are now ALL Admin-only (see Section 0). Do this before any "
        "training — role determines what each person sees.")
story.append(checklist([
    ("Create all user accounts (Users page or CSV import, dry-run first). Have a <b>12+ char complex password</b> ready for every non-operator; operator rows auto-generate one (badge login).",
     "Weak passwords are refused (422) — e.g. “Password1234!” is rejected because it contains “password”."),
    ("Assign roles deliberately: estimators/planners = Supervisor+, purchasing approver = Manager+, receiving = Supervisor+, inspectors = Quality, shipping clerks = Shipping. Do NOT give office staff the Operator role.", None),
    ("Audit employee IDs so the <b>last 4 digits are unique</b> across all users BEFORE printing badges.",
     "Badge login normalizes to the trailing 4 digits — a collision hard-fails BOTH users (409), and employee_id isn't editable from the Users edit modal afterward."),
    ("Give Admins/Managers <b>non-numeric, non-guessable employee IDs</b>.",
     "Badge login is passwordless for every role — a guessable privileged employee ID is a password-free door."),
    ("Provision a <b>second trained Admin</b> account.",
     "User creation, imports, egress switches, and approvals are all Admin-only; an Admin also can't change their own role. One admin is a single point of failure and a role-change dead-end."),
    ("Print badges (Users → multi-select → Print Badges — Admin-only now). Scanners must be 2D imagers in keyboard-wedge mode with an Enter suffix (1D lasers can't read QR).", None),
    ("Reassign any manager who relied on creating users or printing badges to route those through an Admin.", None),
]))

# ---- 5. Stations ----------------------------------------------------------
section(story, "5", "Station terminals", "OWNER: ADMIN",
        "Pin each device's browser to its URL in kiosk/full-screen with sleep disabled. Tokens/PINs shown once — copy "
        "immediately.")
story.append(checklist([
    ("<b>Single-operator kiosks:</b> pin <font face='Courier'>/kiosk?kiosk=1&amp;work_center_id=N</font> (no server record needed).", None),
    ("<b>Crew stations:</b> Work Centers → Kiosk Stations → create one per terminal (bound work center + 4–8 digit shared PIN), pin the copied <font face='Courier'>/kiosk?kiosk=1&amp;station=&lt;id&gt;</font>. Keep the revocation runbook handy.", None),
    ("<b>Wallboard TVs:</b> Admin Settings → Wallboard Displays → New display; copy the one-time <font face='Courier'>?token=</font> URL immediately (shown once), open on each TV, optionally add <font face='Courier'>&amp;dept=</font>. Calendar the expiry.", None),
    ("<b>Visitor tablet:</b> Visitor Log → Stations → create (label + PIN), open <font face='Courier'>/visitor-signin?station=&lt;id&gt;</font>, enter the PIN once. Run ONE real end-to-end sign-in to test the host check-in email.", None),
    ("Verify Wi-Fi at every station and leave a printed <b>paper fallback form</b> at each kiosk and the lobby.",
     "There is no offline write queue anywhere — an outage hard-disables all recording. Paper + a named back-entry owner is the contingency (see Section 9)."),
    ("Confirm the new visitor <b>Add visit</b> back-entry works (Admin/Manager, real past times) — it needs migration 064 (Section 1).", None),
]))

# ---- 6. Egress ------------------------------------------------------------
section(story, "6", "CUI egress switches — three deliberate decisions", "OWNER: ADMIN \xb7 ALL AUDITED",
        "All three default OFF and are per-company; nothing crosses the boundary until an Admin flips them. Treat "
        "enabling any as a CMMC/CUI sign-off, not a routine toggle.")
story.append(checklist([
    ("<b>allow_ai_egress</b> (Admin Settings → AI Privacy): decide before staff upload real POs/drawings.",
     "A freshly created go-live company starts OFF (only pre-existing companies were grandfathered ON). When ON, PO upload, BOM extraction, Copilot, and NL search send content to Anthropic."),
    ("<b>allow_carrier_egress</b>: if buying labels week 1, configure a carrier account + ship-from profile, run Test connection, then enable. Otherwise carrier buttons 409 by design — put that on the cheat sheet.", None),
    ("<b>allow_print_egress</b> (+ auto_print_on_receipt): if using thermal labels, complete the print profile (base URL incl. /api/v1, target, API key), set is_active, then enable. Do one live test receive.",
     "Both toggles are independent and BOTH required for auto-print; an incomplete profile is a silent no-op / 409. INTEGRATION_ENCRYPTION_KEY (or WEBHOOK_ENCRYPTION_KEY) must be set or storing the key fails in prod."),
]))

# ---- 7. Per-role readiness ------------------------------------------------
section(story, "7", "Per-role readiness check", "OWNER: ADMIN + FLOOR CHAMPION",
        "Before each group trains, confirm one real person in that role can do their day-1 tasks end to end.")
table(story, ["Role", "Confirm they can…"], [
    ["Operator (kiosk)", "Badge in, clock in a real queued job, report production with a scrap reason, record a process step, complete."],
    ["Planner (Supervisor+)", "Create a WO from a released routing and RELEASE it. Print a traveler."],
    ["Purchasing (Manager)", "Create a PO against an approved vendor and SEND it (Manager/Admin only)."],
    ["Receiving (Supervisor+)", "Receive against an imported open PO with a lot number; print the 4×6 label (or see 409=egress off)."],
    ["Shipping", "Create a shipment and mark shipped (terminal — closes the WO)."],
    ["Quality", "Disposition an NCR, run a receiving inspection, approve labor (no self-approval)."],
    ["Front desk", "Unlock the visitor tablet, sign a visitor in/out; Admin/Manager can Add visit + staff-sign-out."],
], [0.24, 0.76])

# ---- 8. Training schedule -------------------------------------------------
section(story, "8", "Training schedule", "OWNER: ADMIN + CHAMPIONS",
        "45–60 min hands-on sessions doing real work, small groups (≤6). Stagger logins — email login is 5/min, "
        "badge login 3/min, per building IP (crew stations allow 30/min).")
table(story, ["Day", "Wave"], [
    ["Mon 7/13", "Cutover execution (AM) → floor-walk verify → office session 1 (planners, purchasing, admins). Declare “Excel retired.”"],
    ["Tue 7/14", "Pilot work center (best crew) + receiving live on real POs. Name the floor champion. Quality session."],
    ["Wed 7/15", "Full floor in waves by shift/work-center (prefer crew stations). Supervisor desktop verbs (resume-from-hold, missed clock-out)."],
    ["Thu 7/16", "Shipping, estimating/sales sessions. Stations polish (wallboard, lobby tablet). Quality deep-cut (SPC/FAI/CoC)."],
    ["Fri 7/17", "Go/no-go review (Section 10). Retro with champions. Decide the paper-mirror sunset date."],
], [0.13, 0.87])

# ---- 9. Support -----------------------------------------------------------
section(story, "9", "Support &amp; incident model", "OWNER: ADMIN",
        "There is no frontend error telemetry — user reports are the alerting system. Active hourly check-ins Mon–Wed.")
subhead(story, "Pre-load these help-desk answers")
story.append(checklist([
    ("<b>RBAC “broken page” wave:</b> Managers see a read-only Users list (write buttons gone by design); Supervisors get /unauthorized on /users; badge printing + account resets now route through an Admin. Not bugs.", None),
    ("<b>Account lockout:</b> 5 failed passwords → 30-min lock, and there is NO admin unlock — wait it out. “Rate limit exceeded” = wait ~60s.", None),
    ("<b>Password rejected (422):</b> needs 12+ chars, upper/lower/number/special, no common words — surprisingly common on an admin reset.", None),
]))
subhead(story, "If the network or a device dies")
story.append(checklist([
    ("Kiosks/lobby hard-disable all recording offline (nothing queues). Keep working the part; write counts/visits on the paper fallback form; a named back-entry owner keys them in the same day, signed in as themselves.", None),
    ("<b>Shop-floor back-entry (post-outage labor):</b> a Supervisor+ flips the ShopFloor “Back-entry (offline catch-up)” toggle ON, then clocks the catch-up in/out so it's excluded from live metrics.",
     "The toggle is EPHEMERAL — it resets to OFF on every page reload. If forgotten, catch-up labor records as LIVE and skews OEE. You cannot back-fill from a crew station (a kiosk token forces source=kiosk)."),
    ("<b>Visitor back-entry:</b> an Admin/Manager uses “Add visit” on the Visitor Log with the real paper times; Supervisors can view/export but not add.", None),
]))

# ---- 10. Go / no-go -------------------------------------------------------
section(story, "10", "Go / no-go scoreboard", "FRIDAY 09:00",
        "Each measure is something the system records — no vibes. Green on 5 of 6 = go; set the paper-mirror sunset "
        "date. Any red gets an owner and a date.")
story.append(checklist([
    ("≥ 80% of floor transactions carry <font face='Courier'>source: kiosk</font> (adoption telemetry).", None),
    ("100% of new work orders created and released in-system; zero Excel edits (spot-check frozen workbook modified dates).", None),
    ("All week's receipts posted against imported POs in-system.", None),
    ("Every active operator shows ≥ 1 audited kiosk action per working day (Audit Log by user).", None),
    ("Labor approvals current within one day; missed clock-outs corrected.", None),
    ("Visitor on-site count reads zero at each day's close.", None),
]))
story.append(Spacer(1, 3))
story.append(callout("<b>Not week-1 gates:</b> OTD / FPY / scrap KPIs — early denominators are empty and render em "
                     "dashes by design. They become meaningful in week 3+.", "navy"))

# ---- 11. Sharp edges ------------------------------------------------------
section(story, "11", "Known sharp edges", "PUT ON THE CHEAT SHEETS",
        "Verified in main today — these are by-design or cosmetic behaviors, not bugs; telling people up front "
        "converts “it's broken” into “yep, known.”")
table(story, ["What happens", "What it is", "What to do"], [
    ["Routing sidebar shows “0 operations”", "COSMETIC — list API returns operation_count; UI reads operations.length", "Ignore — the routing DOES have operations; open the detail pane to see them. No data missing."],
    ["BOM/Routing Release fires instantly", "QUIRK — no confirm dialog (unlike Delete/Unrelease)", "Treat Release as irreversible-on-click; it locks the process for revision control. Brief planners."],
    ["Can't deactivate a Customer or Inventory lot", "GAP — no UI control (Vendors got one in #104; Customers/lots didn't)", "Route to an admin (API/DB); customer soft-delete exists server-side, a lot can only be zeroed via /inventory/adjust."],
    ["Carrier / label buttons 409", "DESIGN — egress switch off", "Expected until enabled (Section 6). Manual shipping/receiving still works."],
    ["Manager can't Add/Edit users or print badges", "DESIGN — #114 made user mgmt Admin-only", "Route through an Admin. Managers keep a read-only list."],
    ["Logged out after ~15 min (office) / ~4 min (kiosk)", "DESIGN — idle + 24h absolute session cap", "Compliance behavior; log back in."],
], [0.30, 0.37, 0.33])

# ---- 12. Week-2 -----------------------------------------------------------
section(story, "12", "Week-2 punch list", "OWNER: ADMIN + ENG",
        "Carry-forward once the floor is stable.")
story.append(checklist([
    ("Smaller code items to schedule: add a BOM/Routing <b>Release confirmation</b> dialog, a Customer/Inventory-lot <b>deactivate</b> UI, and fix the routing “0 operations” label (cosmetic). Consider addressing the costing report's hardcoded $50/hr labor rate.", None),
    ("Backfill operator certifications (Operator Certifications module — not importable; doesn't block day-1 clock-ins).", None),
    ("Complete the remaining IA-password follow-ups if not yet merged (the separate chip): company-register/create validators are in #119; confirm nothing regressed.", None),
    ("Sunset the paper-traveler mirror once the go/no-go metrics hold for a full week; reconcile the final paper batch in.", None),
    ("Front /health/detailed with auth; calendar wallboard token expiries; consider promoting Playwright E2E to a blocking check.", None),
    ("Review scrap-reason codes against what the floor actually picked; refine the vocabulary.", None),
]))

story.append(Spacer(1, 10))
story.append(HRFlowable(width="100%", thickness=0.8, color=LINE, spaceAfter=5))
story.append(Paragraph(
    "WERCO ERP-MES \xb7 Admin Go-Live Master Checklist \xb7 Rev E \xb7 2026-07-13 \xb7 verified against production main. "
    "This is the master; the 16 role/poster/form PDFs in this folder are the per-station handouts.",
    ParagraphStyle("foot", fontName="Courier", fontSize=6.8, leading=9, textColor=LIGHT)))

path = os.path.join(OUT, "00-ADMIN-MASTER-CHECKLIST.pdf")
doc = SimpleDocTemplate(path, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
                        topMargin=42, bottomMargin=52, title="Werco Admin Go-Live Master Checklist",
                        author="Werco Manufacturing")
doc.build(story, onFirstPage=_deco, onLaterPages=_deco)
print("wrote", path)
