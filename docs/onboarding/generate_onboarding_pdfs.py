#!/usr/bin/env python3
"""Generate the go-live quick-reference PDF pack (role cards, posters, cheat sheets,
paper fallback forms, cutover checklist) into docs/onboarding/quick-reference/.

This pack complements the long-form guides in docs/onboarding/*.md — those teach the
system; these are the one-page handouts for workstations, kiosks, and go-live week.

Run:  backend/.venv311/bin/python docs/onboarding/generate_onboarding_pdfs.py

Content was verified against the runbooks and code on 2026-07-12. If system behavior
changes (rate limits, role gates, kiosk flows), update the content dicts below and
regenerate — the PDFs are build artifacts, this script is the source.

NOTE: reportlab's built-in fonts use WinAnsi encoding — stick to ASCII plus the safe
set used below (em dash, middot, bullet, multiplication sign, single guillemet).
"""

import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
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

# ---------------------------------------------------------------- design tokens
NAVY = colors.HexColor("#1B4D9C")
RED = colors.HexColor("#C8352B")
INK = colors.HexColor("#16202E")
GRAY = colors.HexColor("#4B5563")
LIGHT = colors.HexColor("#8A94A3")
LINE = colors.HexColor("#D3D8DF")
PANEL = colors.HexColor("#EEF0F3")
AMBER = colors.HexColor("#8A5A00")
AMBER_BG = colors.HexColor("#FDF3DD")

MARGIN = 46
PAGE_W, PAGE_H = letter

S = {
    "tag": ParagraphStyle("tag", fontName="Courier-Bold", fontSize=8, leading=10, textColor=RED),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=23, textColor=INK, spaceAfter=2),
    "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=9, leading=12, textColor=GRAY),
    "h": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=NAVY),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.3, leading=12.3, textColor=INK),
    "body_gray": ParagraphStyle("body_gray", fontName="Helvetica", fontSize=9.3, leading=12.3, textColor=GRAY),
    "num": ParagraphStyle("num", fontName="Courier-Bold", fontSize=9.5, leading=11, textColor=RED, alignment=1),
    "trap": ParagraphStyle("trap", fontName="Helvetica", fontSize=8.8, leading=11.5, textColor=INK),
    "say": ParagraphStyle("say", fontName="Helvetica-Oblique", fontSize=9, leading=11.5, textColor=GRAY),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.6, leading=11, textColor=INK),
    "cell_gray": ParagraphStyle("cell_gray", fontName="Helvetica", fontSize=8.6, leading=11, textColor=GRAY),
    "th": ParagraphStyle("th", fontName="Courier-Bold", fontSize=7, leading=9, textColor=GRAY),
    "form_label": ParagraphStyle("form_label", fontName="Courier-Bold", fontSize=7.5, leading=9.5, textColor=GRAY),
    "poster_title": ParagraphStyle("poster_title", fontName="Helvetica-Bold", fontSize=13.5, leading=16, textColor=INK),
    "poster_body": ParagraphStyle("poster_body", fontName="Helvetica", fontSize=10.2, leading=13.6, textColor=INK),
    "poster_num": ParagraphStyle("poster_num", fontName="Courier-Bold", fontSize=16, leading=19, textColor=RED, alignment=1),
}


def _decorator(doc_code, page_size=letter):
    w, h = page_size

    def deco(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, h - 6, w, 6, stroke=0, fill=1)
        canvas.setFont("Courier-Bold", 6.5)
        canvas.setFillColor(LIGHT)
        canvas.drawString(MARGIN, h - 24, "WERCO ERP-MES  ·  GO-LIVE WEEK JUL 13-17, 2026  ·  WERCOMFG.APP")
        canvas.drawRightString(w - MARGIN, h - 24, doc_code)
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, h - 31, w - MARGIN, h - 31)
        canvas.line(MARGIN, 42, w - MARGIN, 42)
        canvas.setFont("Courier", 6.5)
        canvas.drawString(MARGIN, 32, "QUESTIONS: FLOOR CHAMPION › ADMIN › JON  ·  SYSTEM OF RECORD: THE ERP, NOT PAPER")
        canvas.drawRightString(w - MARGIN, 32, "REV A · 2026-07-12 · PAGE %d" % doc.page)
        canvas.restoreState()

    return deco


def title_block(story, tag, title, sub):
    story.append(Paragraph(tag, S["tag"]))
    story.append(Spacer(1, 3))
    story.append(Paragraph(title, S["title"]))
    story.append(Paragraph(sub, S["sub"]))
    story.append(Spacer(1, 7))
    story.append(HRFlowable(width="100%", thickness=1.2, color=INK, spaceAfter=10))


def section(story, heading):
    story.append(Spacer(1, 4))
    story.append(Paragraph(heading.upper(), S["h"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=2, spaceAfter=6))


def steps(story, items, width=PAGE_W - 2 * MARGIN):
    rows = [[Paragraph(str(i + 1), S["num"]), Paragraph(t, S["body"])] for i, t in enumerate(items)]
    t = Table(rows, colWidths=[22, width - 22])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (0, -1), 0.7, RED),
        ("LINEBELOW", (0, 0), (0, -2), 0.7, colors.white),
        ("LINEABOVE", (0, 1), (0, -1), 0.7, RED),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (1, 0), (1, -1), 9),
        ("LINEBELOW", (1, 0), (1, -2), 0.4, LINE),
    ]))
    story.append(t)


def bullets(story, items):
    for t in items:
        story.append(Paragraph("•&nbsp;&nbsp;" + t, S["body"]))
        story.append(Spacer(1, 3.5))


def trap_box(story, text, label="TRAP", width=PAGE_W - 2 * MARGIN):
    p = Paragraph("<font name='Courier-Bold' size='7.5' color='#8A5A00'>%s — </font>%s" % (label, text), S["trap"])
    t = Table([["", p]], colWidths=[4, width - 4])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), AMBER),
        ("BACKGROUND", (1, 0), (1, -1), AMBER_BG),
        ("TOPPADDING", (1, 0), (1, -1), 6),
        ("BOTTOMPADDING", (1, 0), (1, -1), 6),
        ("LEFTPADDING", (1, 0), (1, -1), 8),
        ("RIGHTPADDING", (1, 0), (1, -1), 8),
    ]))
    story.append(Spacer(1, 4))
    story.append(t)
    story.append(Spacer(1, 4))


def build(filename, doc_code, story, page_size=letter, top=48):
    path = os.path.join(OUT, filename)
    doc = SimpleDocTemplate(
        path, pagesize=page_size,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=top, bottomMargin=58,
        title=doc_code, author="Werco Manufacturing",
    )
    deco = _decorator(doc_code, page_size)
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    print("wrote", path)


# ============================================================== 01-09 role cards
ROLE_CARDS = [
    dict(
        file="01-role-card-operator-kiosk.pdf", code="WERCO-OB-01",
        tag="ROLE CARD · OPERATOR · SHOP-FLOOR KIOSK",
        title="Operator — Your Kiosk",
        sub="Your station: the kiosk screen at your work center. You sign in with your badge — no password, ever. "
            "Everything you do is recorded under your name (AS9100D requirement).",
        sections=[
            ("Sign in", "steps", [
                "Scan your badge anywhere on the screen — no need to tap a field first. No scanner handy? "
                "Tap the on-screen number pad and enter your badge number.",
                "The kiosk returns to the badge screen after about 4 minutes idle (a 30-second countdown warns you). "
                "Your work is saved — just badge back in.",
                "Never share badges or work under someone else's login. A badge is a full identity.",
            ]),
            ("Run a job", "steps", [
                "Tap your job in the queue, confirm, <b>CLOCK IN</b>. Two taps.",
                "Job has steps? Tap <b>REVIEW STEPS</b> on the confirm card before you start.",
                "<b>REPORT PRODUCTION</b> during the run: good count + scrap count. Any scrap requires a reason tile — "
                "the server refuses scrap without one.",
                "Record process steps as you go — the “Steps 2/6” chip tracks required vs. recorded.",
                "Done? <b>COMPLETE</b> clocks you out with final counts, then completes the operation.",
                "Stuck? <b>HOLD</b> — pick the blocker category (material, machine, tooling, quality...). "
                "A supervisor resumes it from the desktop; there is no resume button on the kiosk.",
            ]),
            ("Fix a mistake", "bullets", [
                "Never retype over a recorded value. Tap <b>CORRECT</b>: give a reason plus the new value. "
                "The original stays visible marked “superseded” — that is required evidence, not a bug.",
            ]),
            ("If the screen says OFFLINE", "bullets", [
                "Buttons gray out and nothing is queued in the background. Keep running the part; write counts on the "
                "paper fallback form at your station. When the banner clears, press the action again — what you typed is still there.",
            ]),
        ],
        traps=[
            "Hitting target and walking away: clocking out at target always saves your hours, but the job stays "
            "IN PROGRESS if step records are still owed. Read the toast — finish the steps, then COMPLETE.",
            "“Previous operations must be completed first” means the job is not ready for you yet — "
            "check with your lead instead of retrying.",
        ],
    ),
    dict(
        file="02-role-card-operator-crew-station.pdf", code="WERCO-OB-02",
        tag="ROLE CARD · OPERATOR · CREW STATION",
        title="Operator — Crew Station",
        sub="A shared terminal for crews working the same operation. A PIN unlock lasts up to 24 hours (or until "
            "someone locks the station); every action is authorized by scanning YOUR badge — records always land "
            "under the scanned name.",
        sections=[
            ("The station", "steps", [
                "First person in unlocks the station with the shared PIN. The unlock lasts up to ~24 hours — "
                "unless someone taps LOCK STATION, after which anyone re-enters the PIN.",
                "Tap <b>LOCK STATION</b> when leaving it unattended — idle never locks it by itself.",
                "After 90 seconds idle, a half-entered screen resets back to the crew board. Nothing partial is saved — "
                "just start the action again.",
            ]),
            ("Join, work, leave", "steps", [
                "Tap a job, then scan <b>your</b> badge — the station decides: not on the roster = JOIN (clock-in); "
                "already on it = LEAVE (clock-out).",
                "Badge sessions last 5 minutes. A mid-task “scan badge again” prompt is normal — scan and keep going.",
                "REPORT PRODUCTION, HOLD, and COMPLETE all finish with a badge-signature scan; STEPS asks for "
                "your badge up front and records everything as “Recording as {your name}.”",
                "<b>COMPLETE</b> clocks out the <b>whole crew</b> — the dialog names everyone it is closing out. "
                "Enter final pieces first if prompted.",
            ]),
            ("The counting rule", "bullets", [
                "Crew quantities <b>add up</b> across everyone — enter only <b>NEW</b> pieces you have not reported yet. "
                "The banner “CREW TOTAL SO FAR: 37 of 50” is the shared running total; the server does not "
                "de-duplicate double reports.",
            ]),
        ],
        traps=[
            "The out-of-tolerance HOLD + FILE NCR button closes everyone's open time entries. Your hours are not "
            "lost — they are saved and closed. Badge back in when the job resumes.",
        ],
    ),
    dict(
        file="03-role-card-production-planner.pdf", code="WERCO-OB-03",
        tag="ROLE CARD · PRODUCTION PLANNER · SUPERVISOR+",
        title="Production Planner",
        sub="Pages: Work Orders · Scheduling · Dashboard · Action Inbox. "
            "You are the office–floor handoff: nothing reaches an operator's queue until you release it.",
        sections=[
            ("Create and release", "steps", [
                "Create the WO at Work Orders › New. Auto-routing copies the part's <b>released</b> routing; "
                "“No released routing found” means get it released or add operations manually.",
                "Serialized job? Enter serial numbers one per line — the count must equal quantity ordered.",
                "<b>RELEASE</b> the work order. Only released WOs appear in floor queues. Release is draft-only and "
                "needs at least one operation.",
                "Print the traveler (QR-coded). Traveler QRs are lookup-only today — operators still clock in "
                "from the kiosk queue, not by scanning.",
                "Watch the Dashboard and Action Inbox for holds and blockers; resume held operations from the desktop.",
            ]),
            ("When the server says no", "bullets", [
                "409 “process sheet unavailable”: the routing points at a sheet with no released revision. "
                "The fix is release the sheet or detach it — retrying will not help.",
                "Refusal messages (“Only draft work orders can be released”...) are the system enforcing a rule. "
                "The text is the real reason — read it.",
            ]),
        ],
        traps=[
            "KNOWN BUG: Create can silently do nothing when auto-populated run times are not in clean 0.1 steps "
            "(e.g. 9.99). If Create “doesn't work,” re-enter run times as 0.1-aligned values.",
            "Release has no undo — a wrongly released WO is visible to the floor immediately.",
        ],
    ),
    dict(
        file="04-role-card-purchasing.pdf", code="WERCO-OB-04",
        tag="ROLE CARD · PURCHASING · SUPERVISOR / MANAGER",
        title="Purchasing",
        sub="Pages: Purchasing · Upload PO · MRP. The approved supplier list (VND-001...VND-100) is already loaded.",
        sections=[
            ("Buy", "steps", [
                "Create a PO on the Purchasing page against an approved vendor; add lines (part, quantity, price, "
                "promised date). Supervisor and up can build POs.",
                "<b>SEND</b> to the vendor is Manager/Admin only — supervisors build, a manager issues. "
                "Only draft or approved POs can be sent.",
                "Upload PO: drop a vendor document and the AI extracts it — but the created PO lands directly in "
                "<b>SENT</b> (issued, immediately receivable), with no manager Send step. Review the extracted data "
                "carefully before clicking Create. Needs the AI egress switch ON — a “disabled for your company” "
                "message means the admin has it off, not a fault.",
                "Check MRP for shortages before you buy.",
            ]),
            ("Vendors", "bullets", [
                "Vendor create/edit is Manager and up. Approving a vendor stamps its approval date.",
                "There is no delete button for vendors — a mis-entry goes to the admin. "
                "Never create a duplicate as a workaround.",
            ]),
        ],
        traps=[
            "Bulk-imported POs land directly in SENT status — immediately receivable. A typo'd import instantly "
            "“issues” POs, which is why imports are admin-driven.",
        ],
    ),
    dict(
        file="05-role-card-receiving.pdf", code="WERCO-OB-05",
        tag="ROLE CARD · RECEIVING · SUPERVISOR+",
        title="Receiving",
        sub="Page: Warehouse › Receiving &amp; Inspection tab. Lot numbers captured here are the traceability record.",
        sections=[
            ("Receive", "steps", [
                "Pick the open PO, pick the line — quantity defaults to the remaining amount.",
                "Receive. Enter lot numbers with care — they follow the material through every job (traceability).",
                "Receiving more than the remaining quantity requires the over-receive approval checkbox.",
                "Print the 4×6 receiving label from the success toast or the history row. "
                "A 409 “egress is disabled” means the admin has not enabled label printing — not a printer fault.",
                "Items flagged <b>requires inspection</b> land in the inspection queue. Quality signs those off — "
                "receiving does not.",
            ]),
        ],
        traps=[
            "Auto-print failures are SILENT by design (an incomplete or disabled print profile quietly no-ops). "
            "On your first live receive, physically confirm a label came out of the printer.",
        ],
    ),
    dict(
        file="06-role-card-shipping.pdf", code="WERCO-OB-06",
        tag="ROLE CARD · SHIPPING",
        title="Shipping",
        sub="Page: Warehouse › Shipping tab. One button here is terminal — read step 3 twice.",
        sections=[
            ("Ship", "steps", [
                "Create a shipment from the Ready-to-Ship list: ship-to, quantity, packing notes.",
                "Print the packing slip and shipping paperwork.",
                "<b>MARK SHIPPED is terminal: it closes the work order. There is no un-ship button.</b> "
                "Triple-check the WO number before you click.",
                "Carrier buttons (validate address / rate shop / buy label) return 409 while the carrier egress "
                "switch is off — that is policy, not a bug. Manual shipping works regardless.",
                "A Certificate of Conformance auto-issues on ship when required; minting one manually is a "
                "Quality/Manager action, not Shipping.",
            ]),
        ],
        traps=[
            "A wrong shipment marked shipped = a live work order closed with no UI undo. Slow down on this button.",
        ],
    ),
    dict(
        file="07-role-card-quality.pdf", code="WERCO-OB-07",
        tag="ROLE CARD · QUALITY",
        title="Quality",
        sub="Pages: Quality (NCR / CAR / FAI / Scrap codes) · SPC · Calibration · Traceability. "
            "You own the gates the floor runs into — keep them clean and the floor keeps moving.",
        sections=[
            ("Daily loop", "steps", [
                "Triage NCRs — kiosk-filed out-of-tolerance NCRs arrive pre-filled (spec, actual, part/lot/serial) "
                "in IN PROCESS status.",
                "Work the receiving inspection queue: accept or reject with method and notes.",
                "Approve labor time entries daily — self-approval is refused. Late approvals erode the floor's "
                "trust in the clock.",
                "Escalate an NCR to a CAR when the cause is systemic.",
            ]),
            ("Own the vocabulary and the gates", "bullets", [
                "Scrap reason codes live on Quality › Scrap tab. Retire codes by making them inactive — never delete. "
                "With no active codes the kiosk falls back to a legacy free-text grid.",
                "Calibration: every gauge must be ACTIVE with a next-calibration date, or every gauge-required step "
                "on the floor is refused (fail-closed). Alerts surface 30 days out.",
                "SPC fills itself from conforming wired measurements. A correction adds a NEW point — "
                "it is a time series, not a rewrite.",
                "FAI prefill-from-steps copies actuals and the measuring device where the characteristic description "
                "exactly matches the step label. It never sets conformance — you disposition every characteristic.",
            ]),
        ],
        traps=[
            "Unqualified-operator warnings do NOT block clock-ins — the system records the exception on the audit "
            "trail instead of stopping the work. Review those warnings; nothing was prevented.",
            "You are the custodian of force-complete: the audited steps-gate bypass exists for legacy and "
            "paper-evidenced jobs only. Habitual use quietly erodes the AS9100D evidence posture.",
        ],
    ),
    dict(
        file="08-role-card-estimating-sales.pdf", code="WERCO-OB-08",
        tag="ROLE CARD · ESTIMATING &amp; SALES · SUPERVISOR+",
        title="Estimating &amp; Sales",
        sub="Pages: Quotes · AI RFQ Quote · Estimate Workbench · Quote Calculator · Customers · Shop Data.",
        sections=[
            ("Quote to work order", "steps", [
                "Quotes page: create, add lines, <b>SEND</b> to the customer. When accepted, <b>CONVERT</b> to a "
                "work order (Supervisor and up).",
                "AI RFQ: upload the RFQ package (PDF / XLSX / DXF / STEP), Generate AI Estimate, then review the "
                "assumptions and the Missing / Needs-Review list — the parser never invents required fields. "
                "Approve &amp; Create Quote when satisfied.",
                "Estimate Workbench: build lines, Recalc, clear <b>every</b> review flag, then Finalize — "
                "it stays blocked while any calc error remains. Export the audit trail with the estimate.",
                "The customer PDF excludes operation-level times; the internal export includes them. Send the right one.",
            ]),
        ],
        traps=[
            "Both estimate engines price from Shop Data rates and quote settings — verify those reflect real shop "
            "rates before quoting real work this week.",
            "RFQ parsing is deterministic and works even with AI egress off; PO Upload and Copilot do need egress ON.",
        ],
    ),
    dict(
        file="09-role-card-front-desk.pdf", code="WERCO-OB-09",
        tag="ROLE CARD · FRONT DESK · RECEPTION",
        title="Front Desk",
        sub="Devices: the lobby visitor tablet, plus the Visitor Log page in the office. "
            "The visitor record is AS9100D / CMMC evidence — the end-of-day zero matters.",
        sections=[
            ("The tablet", "steps", [
                "Each morning, unlock the tablet with the station PIN — the session then lasts 24 hours.",
                "Visitors self-serve: Sign In needs a name, a purpose tile, and the safety/NDA checkbox "
                "(“Other” purpose needs a note). Company, phone, and host are optional.",
                "Host check-in emails only fire on an exact full-name match — help visitors spell the host's name.",
                "Sign Out is by typed name; duplicates show a picker. “No open visitor record found” means they "
                "never signed in or were already signed out.",
                "The form wipes itself after 120 seconds idle (privacy) — the visitor just starts over. "
                "Tap <b>LOCK STATION</b> at end of day.",
            ]),
            ("End of day", "steps", [
                "Open the Visitor Log page: the on-site count must read <b>zero</b> at close.",
                "Sign out stragglers with the staff sign-out button (Admin/Manager).",
            ]),
        ],
        traps=[
            "Tablet OFFLINE = no electronic sign-in, and nothing queues. Use the paper visitor form — "
            "it is a compliance record — and back-enter the visits the same day.",
        ],
    ),
]


def build_role_cards():
    for card in ROLE_CARDS:
        story = []
        title_block(story, card["tag"], card["title"], card["sub"])
        for heading, kind, items in card["sections"]:
            section(story, heading)
            if kind == "steps":
                steps(story, items)
            else:
                bullets(story, items)
            story.append(Spacer(1, 4))
        section(story, "Watch out")
        for t in card["traps"]:
            trap_box(story, t)
        build(card["file"], card["code"], story)


# ============================================================ 10 refusals poster
def build_refusals_poster():
    story = []
    title_block(
        story,
        "KIOSK POSTER · PRINT AT EVERY STATION",
        "Five Things the System Will Refuse",
        "These are quality rules enforced by the server — not errors, and not the kiosk being broken. "
        "There are no operator overrides. Here is what each one means and what to do.",
    )
    refusals = [
        ("Scrap needs a reason",
         "Any scrap quantity without a reason is rejected — on production reports <b>and</b> on clock-outs. "
         "Zero scrap needs nothing.",
         "Say it like this: “Pick the reason tile — then it goes through.”"),
        ("Out of tolerance is not recorded",
         "The server refuses the value; the on-screen preview is advisory only. Re-measure — or if the part truly is "
         "out, tap <b>HOLD + FILE NCR</b>: it files a pre-filled NCR, holds the job, clocks out everyone's open labor, "
         "and shows the NCR number big enough to tag the part.",
         "Say it like this: “The red strip isn't an error — it's a decision point.”"),
        ("Gauge refused",
         "Gauge-required steps demand scanning a calibration-current gauge first. Out-of-cal, inactive, unknown, or "
         "no-due-date gauges are refused before the value is even checked.",
         "Say it like this: “Get a current gauge — don't retype the same code.”"),
        ("“Previous operations must be completed first”",
         "Sequence gating blocks clock-in while earlier operations at <b>other</b> work centers are still open. "
         "Earlier operations at your own work center do not block you.",
         "Say it like this: “The job isn't ready for you yet — check with your lead.”"),
        ("COMPLETE can be refused — clock-out never is",
         "Completing with required step records missing is refused with a list of exactly what is owed. But clocking "
         "out at target always saves your time and counts — the job just stays in progress until the steps exist.",
         "Say it like this: “Your hours are never trapped. Finish the steps, then complete.”"),
    ]
    for i, (t, body, say) in enumerate(refusals):
        row = Table(
            [[Paragraph(str(i + 1), S["poster_num"]),
              [Paragraph(t, S["poster_title"]), Spacer(1, 3), Paragraph(body, S["poster_body"]),
               Spacer(1, 3), Paragraph(say, S["say"])]]],
            colWidths=[38, PAGE_W - 2 * MARGIN - 38],
        )
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (0, 0), 1.1, RED),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (1, 0), (1, 0), 12),
            ("LINEBELOW", (1, 0), (1, 0), 0.5, LINE),
        ]))
        story.append(row)
        story.append(Spacer(1, 3))
    story.append(Spacer(1, 6))
    facts = ("KIOSK FACTS · idle logout ~4 min (single-operator) · crew badge sessions last 5 min — re-scan is normal "
             "· corrections = CORRECT with a reason, never retype · OFFLINE = nothing queues; use the paper form "
             "· supervisors resume holds from the desktop, not the kiosk")
    t = Table([[Paragraph(facts, ParagraphStyle("facts", fontName="Courier-Bold", fontSize=7.5, leading=11, textColor=GRAY))]],
              colWidths=[PAGE_W - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(t)
    build("10-poster-five-refusals.pdf", "WERCO-OB-10", story)


# ======================================================= 11 sharp edges cheatsheet
def build_sharp_edges():
    story = []
    title_block(
        story,
        "CHEAT SHEET · OFFICE &amp; CHAMPIONS",
        "Known Sharp Edges",
        "Verified open issues and by-design behaviors, so “it's broken” becomes “yep, known.” "
        "Anything not on this list: capture the exact error text, the page, and the time, and escalate.",
    )
    rows = [
        ("New WO Create button “does nothing”", "BUG — silent no-op on non-step-aligned run times",
         "Re-enter run times in clean 0.1 increments"),
        ("Work Order Costing report errors", "BUG — known server error (500)",
         "Skip it this week; use the Job Costing page"),
        ("Routing sidebar shows “0 operations”", "BUG — display-only miscount",
         "Open the routing itself for the real list"),
        ("BOM / Routing Release fires instantly", "QUIRK — no confirmation dialog",
         "Treat Release as final; a mistake means a new revision"),
        ("Can't delete a customer / vendor / lot", "GAP — no UI deactivate path",
         "Route mis-entries to the admin; never work around with duplicates"),
        ("“Rate limit exceeded” at login", "DESIGN — 5/min email, 3/min badge, per building IP",
         "Wait about 60 seconds; don't hammer the button"),
        ("“Account is locked”", "DESIGN — 5 failed passwords locks 30 minutes",
         "No admin unlock exists — wait it out, then try once"),
        ("Carrier / label buttons return 409", "DESIGN — egress kill switch is off",
         "Expected until the admin enables it; manual flows still work"),
        ("Wallboard KPIs show an em dash", "DESIGN — empty denominator, not zero",
         "Normal early in the week; never a week-1 metric"),
        ("Operator “can't find” Purchasing etc.", "DESIGN — role-gated navigation",
         "Working as intended"),
        ("Logged out mid-coffee / blank login page", "DESIGN — 15-min idle logout, 24-hour session cap",
         "Compliance behavior — log back in"),
        ("Leftover TEST records (TES001, TEST-CLQA-001...)", "CLEANUP — QA artifacts pending deactivation",
         "Never quote, receive, or issue against them"),
    ]
    data = [[Paragraph("WHAT HAPPENS", S["th"]), Paragraph("WHAT IT IS", S["th"]), Paragraph("WHAT TO DO", S["th"])]]
    for a, b, c in rows:
        data.append([Paragraph(a, S["cell"]), Paragraph(b, S["cell_gray"]), Paragraph(c, S["cell"])])
    w = PAGE_W - 2 * MARGIN
    t = Table(data, colWidths=[w * 0.34, w * 0.33, w * 0.33], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    build("11-cheatsheet-known-sharp-edges.pdf", "WERCO-OB-11", story)


# ========================================================= 12 login & sessions card
def build_login_card():
    story = []
    title_block(
        story,
        "QUICK CARD · EVERYONE",
        "Logging In &amp; Staying In",
        "wercomfg.app · Two ways in, and every timeout below is compliance behavior — not a bug.",
    )
    section(story, "Two ways in")
    bullets(story, [
        "<b>Office staff:</b> Email + password at wercomfg.app/login.",
        "<b>Shop floor:</b> tap the “Badge ID” toggle and scan or type your badge number — no password. "
        "Kiosk screens go straight to the badge prompt.",
        "Passwords: at least 12 characters with upper + lower + number + special, no common words. "
        "Only you or an Admin can change it.",
    ])
    section(story, "Timeouts you will meet")
    bullets(story, [
        "<b>Office app: 15 minutes idle</b> logs you out (a 60-second warning appears first). "
        "<b>Kiosk screens: about 4 minutes idle</b> returns to the badge prompt — just scan back in.",
        "<b>24 hours absolute:</b> everyone re-logs-in at least daily, no matter what.",
        "<b>5 wrong passwords = locked 30 minutes.</b> There is no admin unlock — wait it out, then try once.",
        "<b>“Rate limit exceeded”</b> = too many logins from the building within a minute. "
        "Wait about 60 seconds; don't hammer the button.",
    ])
    section(story, "Find your way")
    bullets(story, [
        "Your first <b>desktop</b> login auto-plays the 4-step Getting Started tour (kiosk screens skip it — "
        "operators get hands-on training instead). Skipping is fine — replay any tour from the "
        "<b>? Help &amp; Tours</b> menu in the top bar (desktop only).",
        "<b>Ctrl+K</b> finds any part, WO, customer, PO, quote, or vendor. Try a phrase like "
        "“late laser jobs waiting on material.”",
        "<b>Ctrl+/</b> keyboard shortcuts · <b>Ctrl+.</b> Copilot assistant · <b>Esc</b> closes.",
        "All timestamps display in Central time.",
    ])
    section(story, "Access requests")
    bullets(story, [
        "New colleague? Self-registration creates an <b>inactive</b> account an Admin must approve with the right "
        "role — route new-hire access to the admin, not to a shared login.",
        "Leavers are deactivated, never deleted (audit trail).",
        "<b>Lost badge = report it immediately.</b> A badge alone is a full login for that person.",
    ])
    build("12-card-login-and-sessions.pdf", "WERCO-OB-12", story)


# ======================================================== 13 support & escalation
def build_support_card():
    story = []
    title_block(
        story,
        "QUICK CARD · EVERYONE",
        "Getting Help During Go-Live Week",
        "There is no automatic error reporting from your screen — if something looks wrong, "
        "saying so IS the alerting system. Daily 15-minute standup at 07:00 at the pilot work center.",
    )
    section(story, "The ladder")
    steps(story, [
        "<b>Your floor champion or office lead</b> — first stop. They carry the role cards and the "
        "Known Sharp Edges sheet; most “is this broken?” questions end here.",
        "<b>The admin</b> — anything involving accounts, roles, badges, stations, imports, or the egress switches.",
        "<b>Jon</b> — suspected real bugs, data problems, or anything the admin can't clear. "
        "Hotfixes and rollbacks happen at this level.",
    ])
    section(story, "When you report a problem, capture")
    bullets(story, [
        "The <b>exact error text on screen</b> — it is the server's real message, not decoration.",
        "The page you were on, the time it happened, and your name.",
        "What you clicked right before. A photo of the screen beats a description.",
    ])
    section(story, "Sixty-second scripts")
    bullets(story, [
        "“Account is locked” › it unlocks itself after 30 minutes; nobody can rush it.",
        "“Rate limit exceeded” › wait about a minute, try once.",
        "Carrier / label button shows an error saying egress “is disabled for this company” › that feature is "
        "switched off by policy, not broken.",
        "A dash instead of a number on the TV board › not enough data yet, not zero.",
    ])
    section(story, "If the network or a device dies")
    bullets(story, [
        "Kiosks and the lobby tablet <b>do not queue anything offline</b> — buttons disable. Keep working the part.",
        "Write counts / visits on the <b>paper fallback form</b> at the station (every kiosk and the lobby has one).",
        "Hand completed sheets to the back-entry owner the same day — they key records in signed in as themselves, "
        "so the record stays attributable.",
        "Dead tablet: swap hardware and tell the admin — stations can be revoked and re-issued in minutes.",
    ])
    build("13-card-support-escalation.pdf", "WERCO-OB-13", story)


# ===================================================== 14/15 paper fallback forms
def _form_grid(headers, col_fracs, n_rows, page_w):
    w = page_w - 2 * MARGIN
    widths = [w * f for f in col_fracs]
    data = [[Paragraph(h, S["th"]) for h in headers]]
    for _ in range(n_rows):
        data.append(["" for _ in headers])
    t = Table(data, colWidths=widths, rowHeights=[18] + [27] * n_rows)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PANEL),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _form_header_line(items, page_w):
    parts = []
    for label in items:
        parts.append("<font name='Courier-Bold' size='7.5' color='#4B5563'>%s</font>  ________________________" % label)
    return Paragraph("      ".join(parts), S["body"])


def build_production_fallback_form():
    page = landscape(letter)
    story = []
    title_block(
        story,
        "PAPER FALLBACK · USE ONLY WHEN THE KIOSK IS OFFLINE",
        "Production Recording — Offline Fallback",
        "Paper mirrors the system; it is not the system. Hand this sheet to the back-entry owner the same day — "
        "records are keyed in by a signed-in person so they stay attributable.",
    )
    story.append(_form_header_line(["DATE", "WORK CENTER", "REASON", "SHEET BY"], page[0]))
    story.append(Spacer(1, 10))
    story.append(_form_grid(
        ["OPERATOR (NAME / BADGE)", "WO #", "OP SEQ", "CLOCK IN", "CLOCK OUT", "QTY GOOD", "QTY SCRAP",
         "SCRAP REASON (REQUIRED IF ANY)", "STEPS / NOTES"],
        [0.16, 0.09, 0.06, 0.08, 0.08, 0.07, 0.07, 0.19, 0.20],
        11, page[0],
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<b>Back-entry scope:</b> quantities good/scrap (scrap always needs its reason — the system refuses it "
        "without one, so fill that column now) and notes are keyed into the work order's operation. The system "
        "stamps its own times and cannot re-create clock-ins — copy the operator names and clock times into the "
        "entry notes as “paper backfill per WERCO-OB-14,” and keep this filed sheet as the labor time record for "
        "the outage. Nothing back-entered ever shows up as kiosk activity.",
        S["body_gray"]))
    build("14-form-production-fallback.pdf", "WERCO-OB-14", story, page_size=page)


def build_visitor_fallback_form():
    page = landscape(letter)
    story = []
    title_block(
        story,
        "PAPER FALLBACK · USE ONLY WHEN THE LOBBY TABLET IS OFFLINE",
        "Visitor Log — Offline Fallback",
        "This sheet is an AS9100D / CMMC visitor-control record. Back-enter every visit through the tablet's "
        "own Sign In screen once it is back online (the Visitor Log page has no add-visit form), then file this "
        "sheet — do not discard it.",
    )
    story.append(_form_header_line(["DATE", "STATION", "COMPLETED BY"], page[0]))
    story.append(Spacer(1, 10))
    story.append(_form_grid(
        ["TIME IN", "VISITOR NAME", "COMPANY", "HOST (WHO THEY'RE SEEING)",
         "PURPOSE (MEETING / DELIVERY / CONTRACTOR / INTERVIEW / AUDIT / OTHER — IF OTHER, NOTE THE REASON)",
         "SAFETY/NDA ACK (INITIALS)", "TIME OUT"],
        [0.08, 0.17, 0.14, 0.17, 0.25, 0.10, 0.09],
        11, page[0],
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "The safety/NDA acknowledgment initials are required, and an “Other” purpose needs its reason written in — "
        "the electronic form refuses a back-entered visit missing either. Back-entered records carry the entry "
        "time, not the visit time, so this filed sheet remains the authoritative time record. Every visitor must "
        "have a time out before end of day (staff sign-out from the Visitor Log page for stragglers).",
        S["body_gray"]))
    build("15-form-visitor-fallback.pdf", "WERCO-OB-15", story, page_size=page)


# ========================================================== 16 cutover checklist
def _checklist(story, items, width=PAGE_W - 2 * MARGIN):
    rows = [["", Paragraph(t, S["body"])] for t in items]
    t = Table(rows, colWidths=[16, width - 16])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (0, 0), 0.8, GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (1, 0), (1, -1), 9),
        ("LINEBELOW", (1, 0), (1, -2), 0.4, LINE),
    ] + [("BOX", (0, i), (0, i), 0.8, GRAY) for i in range(len(items))]))
    # shrink the checkbox: draw as an 10x10 box via cell size — approximate with padding
    story.append(t)


def build_cutover_checklist():
    story = []
    title_block(
        story,
        "ADMIN RUNSHEET · MONDAY JUL 13",
        "Cutover Day Checklist",
        "Execute the rehearsal migration log — this is a script, not an adventure. Dry-run, read the preview, "
        "then commit, on every step. The Import Center is admin-only; run everything signed in as yourself.",
    )
    section(story, "Before the first import")
    _checklist(story, [
        "Excel frozen in writing last night; workbooks are read-only from that moment.",
        "Fresh files exported from the FROZEN workbooks (not rehearsal copies).",
        "Rehearsal migration log in hand (file names, expected counts, fixes).",
        "Work-center types configured in Admin Settings (step 1 rejects unknown types).",
        "Second admin briefed and reachable.",
    ])
    section(story, "The eleven steps — dry-run, verify preview, commit, verify counts")
    _checklist(story, [
        "<b>1 · Work Centers</b> — import-csv; created count matches file rows.",
        "<b>2 · Users / Operators</b> — non-operator rows need passwords (or the Default Password box); "
        "operators auto-generate (badge login). Employee IDs already audited for trailing-4 uniqueness.",
        "<b>3 · Customers</b> — import-csv; spot-check three records.",
        "<b>4 · Vendors</b> — already loaded (VND-001...VND-100). Verify list intact; print/export codes for step 10.",
        "<b>5 · Parts</b> — import-csv. Part numbers must not collide with materials (shared master table).",
        "<b>6 · Materials &amp; Supplies</b> — import-csv.",
        "<b>7 · BOMs</b> — BOM page wizard (reads all sheets). Check the preview line-by-line: rows after a "
        "1,000-blank-row gap are silently dropped.",
        "<b>8 · Routings</b> — Routing page wizard; assign work centers in the preview dropdowns. "
        "<b>All land DRAFT: release every routing for parts with open WOs before step 11.</b> Budget real time here.",
        "<b>9 · Inventory</b> — no bulk import exists. Key the physical count via Warehouse › Inventory › "
        "Receive (captures lot numbers) or Adjust.",
        "<b>10 · Open POs</b> — vendor_code and part_number must exist; rows sharing a po_number become one PO; "
        "imported POs land in SENT (immediately receivable). Only still-open POs.",
        "<b>11 · Open WOs</b> — requires a RELEASED routing per part; completed_through_seq = last operation "
        "finished on paper (it seeds the queue position; no fake timestamps or labor). Land RELEASED, in queues immediately.",
    ])
    section(story, "After step 11 — nothing else happens until these pass")
    _checklist(story, [
        "Walk the floor with a supervisor: every work center's queue matches reality; each job sits at the right "
        "operation (wrong completed_through_seq is the usual culprit).",
        "Warehouse › Receiving lists the imported open POs.",
        "First operator clock-ins on real queues succeed.",
        "Declare in writing: <b>“Excel is retired as of Jul 13.”</b> Paper travelers may mirror the system "
        "during transition (system stays source of truth); the workbooks are never updated again.",
    ])
    tail = []
    section(tail, "If a commit times out")
    bullets(tail, [
        "Do NOT re-commit blind. Re-run Validate: rows that already landed show as “already exists,” "
        "telling you exactly what is left. Fix failed rows in a NEW file containing only those rows.",
    ])
    story.append(KeepTogether(tail))
    build("16-checklist-cutover-day.pdf", "WERCO-OB-16", story)


if __name__ == "__main__":
    build_role_cards()
    build_refusals_poster()
    build_sharp_edges()
    build_login_card()
    build_support_card()
    build_production_fallback_form()
    build_visitor_fallback_form()
    build_cutover_checklist()
    print("done: 16 PDFs in", OUT)
