# Security review 2026-09: decisions and actions for Kris

The review is in `reports/security-review-2026-09.md`. The code fixes are done (highs, and the mediums in batches A1, A2, B, C and D). What's left needs a decision, an account setting, money or a solicitor. Each item has one recommendation.

## Needs a decision

| # | What | Recommendation | Cost / effort |
|---|---|---|---|
| M20 | The mood hold (a heated thread is held even on full-auto) only runs on Pro and above. The public pages now say so. | Keep the pages as they are now. The other option: run the mood read for every full-auto business as a safety control, with a small AI cost on Starter. | Nil now |
| M23 | A cancel or move request changes the diary (and can charge a late fee) when the draft is written, before anyone approves. Skip doesn't undo it. | Build two-phase cancel/move like holds: the draft records the intent, Approve carries it out, Skip drops it. Until then this contradicts "a human approves everything". | ~1 day build |
| M22 | The sender is trusted by their From address alone. An exact-address spoof that lands could move a real customer's booking. | Read the provider's Authentication-Results header. When it fails, withhold customer context and booking tools, force draft-first and show a warning on the thread. | ~½ day |
| M5 | The operator console accepts a password (no second factor). Sessions are now 12 hours and every change is audited. | Remove the password sign-in for `/admin`: Google/Microsoft sign-in only, with 2-step verification switched on for both operator accounts. | ~1 hour code + your 2FA check |

## Needs an account setting (yours)

| # | What | Where |
|---|---|---|
| M7 | Sessions renew for ever. | Supabase → Authentication → Sessions: time-box 30 days, inactivity timeout 7 days. |
| M24 | Two outages came from lapsed billing. | Render, Supabase, Anthropic, OpenAI: a monitored card and billing alerts to two addresses. Keep headroom on Render build minutes. |
| M25 | No automated checks before deploy. | Turn on GitHub Dependabot alerts. I can add a GitHub Actions workflow (pytest, pip-audit, gitleaks). Deploy-on-green then needs Render's auto-deploy switched to a deploy hook. |
| M26 | No uptime alert. | Point any uptime monitor (e.g. UptimeRobot, free) at `https://blam8.com/health?deep=1`. It now answers 503 when the database is down. |

## Needs money

| # | What | Recommendation |
|---|---|---|
| M13 | No managed backup and no tested restore. The one dump sits unencrypted on this PC. | Supabase Pro (~$25/month): daily backups and point-in-time recovery. Keep the weekly script as an off-site second copy, encrypted. Do one restore drill into a scratch project and write down how long it took. |
| M27 | No staging: local runs and agents use production. Local runs no longer start the background jobs (M29). | A second Supabase project as staging, and a local `.env` pointing at it. Worth doing together with M13. |

## Needs a solicitor or your facts

| # | What | What I need from you |
|---|---|---|
| M12 | `/privacy` and `/terms` still show `[LEGAL ENTITY]`, `[REGISTERED ADDRESS]`, `[COMPANY NO.]`, `[ICO REG NO.]` and the "draft" banner. | The four facts. ICO registration is ~£52/year if not done yet. |
| M18 | No Article 28 data-processing agreement. | I can draft a standard DPA (the eight Art 28(3) terms, the sub-processor list with 30-day objection, breach notice, technical measures annex) for a solicitor to check. |
| M19 | No incident-response plan or breach-notice promise. | I can draft `docs/incident-response.md`: severity levels, who to call, the 72-hour clock, the customer notice template. |
| M28 | No register of who can reach production. | A short `docs/access-register.md` (system × person × method × 2FA). I can draft it; you confirm who has what. |

## Done in code this round (for the record)

- **M10:** Tailwind is self-hosted on every page (no third-party script on sign-in or admin sign-in). The CDN has been removed from the CSP.
- **M11:** The sub-processor list now includes OpenAI, Twilio, Google Maps, postcodes.io and Google Fonts, with a dated change line.
- **M14:** The 90-day sweep also clears the notes made from an email (summaries, booking details, search terms). Tested.
- **M15:** Closing an account removes the business's stored files (documents, photos, public banner) and portal chats.
  - Existing orphans are waiting on you: 30 files, 1 public banner and 176 chat rows. Run `python scripts/db/migrations/apply_closure_orphans_migration.py` (dry run), then `--apply`.
- **M16/M17:** An `admin_audit` table records every console change and every email-body view.
- **M26:** `/health?deep=1` checks the database.
- **M29:** Background jobs run on Render only.
- **M31/M32:** Spreadsheets and Word files are checked for zip bombs and size before they are parsed.
- **M38:** The help-centre wording no longer promises role limits beyond what's enforced.

Follow-ups closed on 27 Sep (small gaps):
- **Concierge customer lookup:** it says a health detail is on record but never passes its value to the model, and it won't stage an edit to one.
- **Confirming or binning cards:** a switched-off member can't do either, and staff can only act on their own cards.
- **Notifications:** a narrower member's swipe or "seen" only touches the rows they were shown.

## Watch after deploy

- **M29:** `/health` should show `"jobs": true` on Render. If it shows `false`, the reminders and polling have stopped. Set `RUN_DAILY_JOBS=true` on Render at once.
- **M8:** Connecting a mailbox from the installed iPhone app may now be refused (the consent cookie must come back to the same browser). Test once on the rig.
- **H5:** Render logs may show "pdf parse child ended without a result". That line means the PDF memory cap bit.
