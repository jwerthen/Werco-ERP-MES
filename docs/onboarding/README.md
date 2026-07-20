# Werco ERP-MES — Employee Onboarding & Training

Welcome to Werco ERP-MES, the system the shop uses to run jobs from quote to ship — work orders, the shop floor, the warehouse, purchasing, quality, and more. This library is your plain-language training set: short, friendly guides that walk you through the screens and buttons you'll actually use. Read the guide for your job, follow the click-by-click steps, and you'll be productive on day one.

> Tip: You don't need to read everything. Read **Getting Started**, then the one guide for your role. Come back to the others only if your job changes.

## Start here

**Everyone reads [01 — Getting Started](./01-getting-started.md) first.** It covers signing in, finding your way around, the Dashboard, search, and the basics that every other guide assumes you already know. Don't skip it.

## Which guides should I read?

Find your job in the left column and read the guides listed next to it.

| Your job | Read these guides |
|---|---|
| Shop-floor operator (machining, fab, weld, paint, assembly, inspection) | Getting Started **+** Operator / Shop-Floor |
| Receiving / Shipping / Inventory clerk | Getting Started **+** Warehouse |
| Buyer / Planner / Supervisor / Manager | Getting Started **+** Planner, Supervisor & Manager (**+** Warehouse if you also receive or ship) |
| Quality | Getting Started **+** Planner, Supervisor & Manager (see its Quality section) |
| Administrator / IT | Getting Started **+** Administrator & IT (**+** the Planner guide for day-to-day operations) |
| Viewer / Executive / Auditor (read-only) | Getting Started |

## The guides

| # | Guide | What it covers | Read | Print |
|---|---|---|---|---|
| 01 | Getting Started | Signing in, navigation, the Dashboard, search and shortcuts — the basics everyone needs | [Markdown](./01-getting-started.md) | [PDF](./pdf/01-getting-started.pdf) |
| 02 | Operator / Shop-Floor | Badge sign-in, the kiosk, clocking time, starting/holding/completing work, recording quantities and scrap | [Markdown](./02-operator-shop-floor.md) | [PDF](./pdf/02-operator-shop-floor.pdf) |
| 03 | Warehouse | Receiving against POs, inspecting and lot capture, inventory and transfers, creating and shipping shipments | [Markdown](./03-warehouse.md) | [PDF](./pdf/03-warehouse.pdf) |
| 04 | Planner, Supervisor & Manager | Creating and releasing work orders, scheduling, purchasing and MRP, quoting, and quality (NCR/CAR/FAI) | [Markdown](./04-planner-supervisor-manager.md) | [PDF](./pdf/04-planner-supervisor-manager.pdf) |
| 05 | Administrator & IT | Creating users and roles, badge provisioning, admin settings, audit log, setup and import tools | [Markdown](./05-administrator-it.md) | [PDF](./pdf/05-administrator-it.pdf) |
| — | Glossary | Plain-language definitions of the terms and acronyms used across the system | [Markdown](./glossary.md) | [PDF](./pdf/glossary.pdf) |

## Quick-reference pack (print these)

Where the guides above *teach* the system, the [`quick-reference/`](./quick-reference/) pack is the set of **one-page printable handouts** for workstations, kiosks, and the lobby — role cards, posters, cheat sheets, offline paper-fallback forms, and the cutover-day runsheet. Content was verified against the runbooks and code on 2026-07-12 (Rev A); regenerate after system changes with:

```bash
backend/.venv311/bin/python docs/onboarding/generate_onboarding_pdfs.py          # handouts 01-16
backend/.venv311/bin/python docs/onboarding/generate_admin_master_checklist.py   # 00 master checklist
```

| # | Handout | Post it at / give it to |
|---|---|---|
| 00 | [Admin go-live master checklist](./quick-reference/00-ADMIN-MASTER-CHECKLIST.pdf) | The admin — the ordered end-to-end go-live runsheet (setup → import → verify → cutover) |
| 01 | [Operator — single kiosk](./quick-reference/01-role-card-operator-kiosk.pdf) | Every single-operator kiosk |
| 02 | [Operator — crew station](./quick-reference/02-role-card-operator-crew-station.pdf) | Every crew station |
| 03 | [Production planner](./quick-reference/03-role-card-production-planner.pdf) | Planners / supervisors |
| 04 | [Purchasing](./quick-reference/04-role-card-purchasing.pdf) | Buyers |
| 05 | [Receiving](./quick-reference/05-role-card-receiving.pdf) | Receiving desk |
| 06 | [Shipping](./quick-reference/06-role-card-shipping.pdf) | Shipping desk |
| 07 | [Quality](./quick-reference/07-role-card-quality.pdf) | Quality staff |
| 08 | [Estimating & sales](./quick-reference/08-role-card-estimating-sales.pdf) | Estimators |
| 09 | [Front desk](./quick-reference/09-role-card-front-desk.pdf) | Reception |
| 10 | [Poster: Five things the system will refuse](./quick-reference/10-poster-five-refusals.pdf) | **Every kiosk** (tours don't run in kiosk mode) |
| 11 | [Cheat sheet: known sharp edges](./quick-reference/11-cheatsheet-known-sharp-edges.pdf) | Office leads & floor champions |
| 12 | [Logging in & staying in](./quick-reference/12-card-login-and-sessions.pdf) | Everyone, day 1 |
| 13 | [Getting help / escalation](./quick-reference/13-card-support-escalation.pdf) | Everyone, day 1 |
| 14 | [Paper fallback: production recording](./quick-reference/14-form-production-fallback.pdf) | Every kiosk (offline contingency) |
| 15 | [Paper fallback: visitor log](./quick-reference/15-form-visitor-fallback.pdf) | Lobby (offline contingency) |
| 16 | [Cutover-day checklist](./quick-reference/16-checklist-cutover-day.pdf) | The admin running the migration |

## How training works

A good path through your first week:

1. Get your account from your administrator or IT. They'll create it and give you your sign-in details. (For shop-floor roles you'll get a badge / employee ID instead of a password.)
2. Read **[01 — Getting Started](./01-getting-started.md)** and sign in for the first time.
3. Read the guide for your role (see the table above).
4. Do the **Try it** drill at the end of your role guide — ideally with your supervisor watching the first time.

### New-hire checklist

- [ ] My account is created and I can sign in (or my badge works at the kiosk).
- [ ] I've read **01 — Getting Started**.
- [ ] I've read the guide for my role.
- [ ] I've completed the **Try it** drill with my supervisor.
- [ ] I know who to ask for help (see my role guide's "Where to get help").

## Glossary and printable handouts

New to a term like NCR, FAI, MRP, traveler, or lot? Check the **[Glossary](./glossary.md)** — it explains the words and acronyms in plain English.

> Tip: Every guide has a **PDF** version (the Print links above). Those are formatted as printable handouts — great for keeping at a workstation or handing out in a training session.
