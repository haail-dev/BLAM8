"""Worked setups — what BLAM8 hands a given kind of business on day one, taken
from the business types we have run end to end on the live system
(docs/e2e-tenant-checklist.md; one run file per type under docs/e2e-run-*.md).

Public pages: /setups (the index) and /setups/{slug} (one per business type).
The angle is not "we beat X" (that's /vs) — it is "this is how YOUR BLAM8 would
be set up": the diary shape, the services and how each is timed, the booking
requirements, the CRM pipeline and remembered facts, the knowledge rules the
agent enforces unprompted, and the customer journey we actually proved.

Honesty rules for every entry (Kris 2026-09-04):
- The businesses are FICTIONAL rigs we built to run the playbook. They are
  worked configurations, never customers or case studies: no quotes, no logos,
  no "trusted by". `business` names the rig; `disclaimer` is rendered on every
  page.
- Every line must have happened in the run it cites. `proven` is the date of
  that run; `run_doc` the notes it came from.
- New entries are added as the LAST step of each reseed (playbook §10).

Pure data + a validate() the tests enforce, so every page has the same sections.

The film (docs/video-playbook.md, 2026-09-20): a type that has been filmed
carries a `video` entry — a short clip of the real product doing the day from
the owner's seat, cut from a take on the live system, plus the same day from
the owner's phone. The clips are cut on Kris's machine and copied into
src/app/static/video/setups/ (the repo is the bucket, as it is for the landing
loop); film() hands the page the entry only once the files have shipped, so a
page never shows a player with nothing behind it.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

TIMING_WORDS = {"time": "exact time", "half": "morning or afternoon", "day": "whole day"}
KINDS = {"answer", "document", "toggle"}

# Where a setup's clips live, served by the /static mount (src/app/main.py). The
# static server answers byte-range requests, which an iPhone needs before it
# will play an mp4 at all — tests/test_setups_pages.py guards that.
VIDEO_DIR = "/static/video/setups/"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# The desktop film: what the viewer watches, when it was filmed, on which rig.
VIDEO_KEYS = {"business", "filmed", "run_doc", "src", "poster", "seconds", "summary", "chapters", "phone"}
# The phone clip beside it: the same day from the owner's phone.
CLIP_KEYS = {"src", "poster", "seconds", "summary"}

SETUPS: list[dict] = [
    {
        "slug": "plumbers",
        "trade": "Plumbing & heating engineers",
        "title": "How a plumbing & heating firm's BLAM8 is set up",
        "business": "Hartwell & Daughters Plumbing & Heating",
        "business_blurb": ("A fictional two-van family firm in Bath, Gas Safe registered: emergency "
                           "callouts, boiler servicing and repairs, landlord gas safety records, new "
                           "boiler installs and bathroom plumbing, within fifteen miles of Bath."),
        "industry": "trades",
        "proven": "4 September 2026",
        "run_doc": "docs/e2e-run-plumbing-2026-09-04.md",
        "hook": "Same-day emergencies, landlord certificates with a tenant on site, and a diary that promises a morning or an afternoon, never an hour.",
        # What this setup's rig has proven since the run — each line is a row in
        # docs/verified-behaviours.md (the rule every setup page keeps: nothing
        # here that isn't on that list). 30 Sep 2026 = film 3, a first sign-up
        # on a clean business, watched end to end.
        "since": [
            {"on": "30 September 2026",
             "text": "From first sign-in to the first confirmed booking in one sitting: a four-turn conversation fills the sign-up form, your inbox connects, your voice is learned from the emails you've already sent, your first facts go in as notes, and the same afternoon a new customer's email is answered in your words, with your price and your parking rule, the details asked for, and \"tomorrow morning\" on the diary — after your one tap."},
            {"on": "30 September 2026",
             "text": "\"Use recommended settings\" then Save gives you a diary that can book: the assistant's card applies the capacity, working days and opening hours it filled, and you confirm them with your own Save."},
            {"on": "30 September 2026",
             "text": "The pre-filled booking checklist can be accepted as it is — \"Looks right — save\" is on screen until you've saved it once."},
            {"on": "30 September 2026",
             "text": "The voice preview never says when you're free. Written before there is a diary, it says it will check and come back — the same rule every real reply keeps."},
            {"on": "30 September 2026",
             "text": "What the assistant recommends reads in your words — \"Working days: Monday to Saturday\", \"Bookings: morning and afternoon windows\", an out-of-hours rule as a sentence with its prices — never a stored code."},
        ],
        "diary": {
            "shape": "Services",
            "shape_why": ("Every job is one of a short menu of fixed-length visits, so the diary runs "
                          "per service. Two vans means two jobs can run at once (capacity 2), with a "
                          "30-minute travel gap between jobs, two hours' notice, and Saturday mornings "
                          "as their own hours. The setup assistant proposed all of this from three "
                          "questions and the owner saved it as offered."),
            "services": [
                {"name": "Emergency callout", "minutes": 60, "timing": "half",
                 "note": "£85 covering the first hour, then £65 an hour. Same day if reported before 2pm."},
                {"name": "Boiler service", "minutes": 90, "timing": "half", "note": "£95."},
                {"name": "Landlord gas safety record (CP12)", "minutes": 60, "timing": "half",
                 "note": "£80, certificate emailed within 24 hours; the tenant is on site, the landlord is billed."},
                {"name": "Boiler repair", "minutes": 120, "timing": "half"},
                {"name": "New boiler install", "minutes": 480, "timing": "day",
                 "note": "Quoted only after a free survey — never priced over email."},
                {"name": "Bathroom plumbing", "minutes": 480, "timing": "day"},
                {"name": "Survey / quote visit", "minutes": 60, "timing": "time",
                 "note": "The one visit with an exact time: the customer is meeting you, not waiting in."},
            ],
        },
        "requirements": {
            "intro": ("Eight details, most of them recommended by the setup assistant in the trade's own "
                      "words — and two of them scoped to particular jobs, so a landlord question is only "
                      "asked on a gas safety record and a boiler question only on boiler work."),
            "fields": [
                {"label": "Full address and postcode", "required": True, "kind": "answer",
                 "note": "Also remembered on the customer record."},
                {"label": "Contact phone number", "required": True, "kind": "answer"},
                {"label": "Description of the problem or job", "required": True, "kind": "answer"},
                {"label": "Access notes", "required": False, "kind": "answer",
                 "note": "Parking, gate codes, whether someone will be home — remembered."},
                {"label": "Landlord or tenant contact details", "required": False, "kind": "answer",
                 "note": "Only asked for a gas safety record — who gets the certificate."},
                {"label": "Boiler make and model", "required": False, "kind": "answer",
                 "note": "Only asked for emergency, service and repair jobs — remembered."},
                {"label": "Photo of the fault or boiler", "required": False, "kind": "document",
                 "note": "Only for emergencies and repairs, as an attachment."},
                {"label": "Previous gas safety record", "required": False, "kind": "document",
                 "note": "Only for a gas safety record; waived gracefully when the landlord can't find it."},
            ],
        },
        "crm": {
            "stages": ["New Enquiry", "Quoted", "Booked In", "Job Done", "Invoiced"],
            "quiet_days": 5,
            "fields": [
                {"label": "Property address", "why": "Where the work is — reused for every job at the same property."},
                {"label": "Boiler make and model", "why": "So the engineer brings the right parts."},
                {"label": "Access notes", "why": "Keys, parking, dogs, who's home."},
                {"label": "Landlord or letting agent", "why": "For rented properties: who gets the certificate and the invoice."},
            ],
        },
        "knowledge": {
            "intro": ("Eight notes. In the run the agent applied the same-day rule, the gas-smell safety "
                      "line and the warranty check without being asked — and one pricing note had to be "
                      "reworded when it quoted the out-of-hours rate for a weekday job."),
            "rules": [
                {"kind": "rate", "text": "Standard call-out £85 covering the first hour, then £65 an hour; out-of-hours (evenings and weekends) £140 for the first hour; boiler service £95; landlord gas safety record £80 with the certificate emailed within 24 hours; parts at cost plus 10%."},
                {"kind": "rule", "text": "All gas work by Gas Safe registered engineers — registration 512345."},
                {"kind": "process", "text": "No heating or hot water, an unstoppable leak or a smell of gas is an emergency: same day if reported before 2pm, first thing next morning after. A smell of gas: call the National Gas Emergency line on 0800 111 999 first, open windows, touch no switches."},
                {"kind": "rule", "text": "New boilers and power flushes are quoted only after a free survey."},
                {"kind": "money", "text": "Payment on completion by card or transfer; landlords and letting agents on 14-day terms."},
                {"kind": "rule", "text": "Bath and fifteen miles around; a £20 travel supplement beyond ten miles."},
                {"kind": "judgement", "text": "A boiler under two years old is under warranty — send the customer to the manufacturer first, don't book a paid repair."},
                {"kind": "process", "text": "Every boiler installed or serviced is due its next annual service eleven months later."},
            ],
        },
        "journey": {
            "intro": "The run we proved on the live system on 4 September 2026, in order.",
            "steps": [
                {"who": "Customer", "what": "Emails: boiler packed up overnight, no hot water, two small kids, a Worcester Bosch showing an EA fault, at an address in Bath. Can someone come today, morning or afternoon? What does a callout cost?"},
                {"who": "BLAM8", "what": "Checks the diary once, holds this afternoon as an arrival window on the first try, and drafts: we can be with you this afternoon, the standard weekday callout is £85 for the first hour then £65, parts at cost plus 10%, the boiler is old enough that warranty isn't a factor, and if you smell gas call 0800 111 999 first. The customer record gains the address, phone, boiler and access notes from the one email."},
                {"who": "Owner", "what": "Approves. The reply is the confirmation; the job lands in the calendar as an afternoon block."},
                {"who": "Customer", "what": "Writes again as a landlord: a gas safety certificate is due at a rented property, the tenant can be in Tuesday or Wednesday morning, send the certificate and invoice to me, not the tenant."},
                {"who": "BLAM8", "what": "Quotes £80 with the certificate inside 24 hours, reserves Tuesday and Wednesday mornings as exclusive offers, asks for the previous certificate as an attachment because this is a gas safety job, and notes that paperwork goes to the landlord."},
                {"who": "Customer", "what": "‘Tuesday morning please’ — and can't find the old certificate."},
                {"who": "BLAM8", "what": "Flips Tuesday to a real booking, releases Wednesday, waives the optional document without fuss, and confirms in one line. The tenant's name and number sit on the job as the person on site."},
                {"who": "Customer", "what": "Opens the portal with the reference from the email footer, asks whether anything is still needed, and asks for a boiler service at the first address one morning next week."},
                {"who": "BLAM8", "what": "Answers from the promise ledger, offers Thursday and Friday mornings as arrival windows, and books Thursday from a single message with no questions — everything needed was already on file. A confirmation email follows."},
                {"who": "Owner", "what": "Sends a quote for a new boiler with an optional smart thermostat and a 25% deposit."},
                {"who": "Customer", "what": "Accepts online by typing their name."},
                {"who": "BLAM8", "what": "Raises the 25% deposit invoice and emails it with the bank details. Money shows the balance still to invoice, net of the deposit."},
            ],
        },
        "gaps": [
            {"text": "A customer with several properties needs facts kept per property: the landlord's second address overwrote the boiler and address remembered for their own home.",
             "built": ("First the damage: a fact read from an email no longer overwrites one on the record — it "
                       "becomes a chip that says what it would replace — and the portal reuses an earlier job's "
                       "boiler or address only for the same place and the same work. Then the feature, the same "
                       "afternoon: Dependants. Name the kind — properties — and what to know about each one, and "
                       "every property is its own record on the landlord's card, with its own boiler, its own "
                       "certificate expiry, and bookings that name which property they're for."),
             "built_on": "4 September 2026"},
            {"text": ("One deal is opened per email thread, so a repeat customer accumulates "
                      "deals and a quote for a new job has none to join."),
             "built": ("A quote now finds its deal by the JOB it is for, not by counting the "
                       "customer's: it follows the booking back to the email that created it "
                       "and lands on the deal that email belongs to. Invoices too. When there "
                       "is no job to follow — a quote written from scratch — the form asks "
                       "which job it is for, and only when the customer genuinely has more "
                       "than one on the go."),
             "built_on": "5 September 2026"},
            {"text": "The tenant read from the landlord's email was filed as an on-site party on the wrong booking.",
             "built": ("A party named in an email now lands on the job that email is about: the bookings the "
                       "email placed or moved are marked as its own job, a stated reference still wins, and "
                       "with no own job and several live ones it refuses to guess."),
             "built_on": "4 September 2026"},
        ],
    },
    {
        "slug": "stonemasons",
        "trade": "Stonemasons & memorial masons",
        "title": "How a stonemason's BLAM8 is set up",
        "business": "Ashworth & Son Stonemasons",
        "business_blurb": ("A fictional third-generation memorial masonry firm in Frome: "
                           "headstones, additional inscriptions, restoration and bespoke "
                           "stonework, working across Somerset churchyards and cemeteries."),
        "industry": "trades",
        "proven": "3 September 2026",
        "run_doc": "docs/e2e-run-stonemason-2026-09-03.md",
        "hook": "Bereaved families, church faculties, and a diary where most visits are an arrival time, not a slot.",
        "diary": {
            "shape": "Services",
            "shape_why": ("Every booking is one of a short menu of fixed-length visits, so the "
                          "diary runs per service. One mason is out at a time (capacity 1) with "
                          "a 30-minute travel buffer between visits, 24 hours' notice, bookable "
                          "90 days ahead."),
            "services": [
                {"name": "Design consultation", "minutes": 45, "timing": "time",
                 "note": "At the workshop — the family comes to you, so it is an appointment at an exact time."},
                {"name": "Site survey", "minutes": 60, "timing": "half",
                 "note": "At the churchyard or property — the customer is told morning or afternoon; you manage the hour."},
                {"name": "Installation day", "minutes": 480, "timing": "day",
                 "note": "A whole-day job, Tuesday to Thursday only, never in frost."},
            ],
        },
        "requirements": {
            "intro": ("Twelve details the agent gathers before a visit is booked — asked in the "
                      "trade's own words, never re-asked once given, with documents requested "
                      "as attachments rather than typed."),
            "fields": [
                {"label": "Type of visit", "required": True, "kind": "answer"},
                {"label": "Full name", "required": True, "kind": "answer"},
                {"label": "Email address", "required": True, "kind": "answer",
                 "note": "Filled from the sender's own address — never asked for by email."},
                {"label": "Phone number", "required": True, "kind": "answer",
                 "note": "Also remembered on the customer record."},
                {"label": "Nature of the work", "required": True, "kind": "answer"},
                {"label": "Inscription wording & lettering style", "required": True, "kind": "answer",
                 "note": "The exact wording, kept verbatim through to the booking."},
                {"label": "Cemetery permit or church faculty", "required": True, "kind": "document",
                 "note": "The granted document, needed before any installation."},
                {"label": "Photos of the existing stonework or site", "required": True, "kind": "document"},
                {"label": "Memorial or loved one's name", "required": False, "kind": "answer"},
                {"label": "Cemetery or site name", "required": False, "kind": "answer"},
                {"label": "Site address", "required": False, "kind": "answer",
                 "note": "Remembered on the customer record for the next visit."},
                {"label": "Access or permissions notes", "required": False, "kind": "answer"},
            ],
        },
        "crm": {
            "stages": ["New enquiry", "Consultation booked", "Design & quote sent",
                       "Approval & permit", "Installation scheduled", "Aftercare & close"],
            "quiet_days": 21,
            "fields": [
                {"label": "Cemetery & plot number", "why": "Needed for every memorial job at the same plot."},
                {"label": "Preferred stone & finish", "why": "Slate, granite, limestone — remembered so it is never asked twice."},
                {"label": "Site address", "why": "The churchyard or property, reused by the portal and the reply drafter."},
            ],
        },
        "knowledge": {
            "intro": ("Eight notes in the knowledge base. The agent quotes the rates itself and "
                      "enforces the rules without being reminded."),
            "rules": [
                {"kind": "rate", "text": "Lawn memorials from £895; full kerbed memorials from £2,150; additional inscription on an existing memorial £110 plus £3.40 per letter; re-gilding from £180; restoration £340 a day plus materials."},
                {"kind": "rule", "text": "No installation is booked until the cemetery permit or church faculty is granted — the firm applies on the family's behalf; approval takes two to four weeks."},
                {"kind": "process", "text": "Every memorial starts with a free design consultation at the workshop and a signed proof of the design and inscription before any cutting."},
                {"kind": "rule", "text": "Installations Tuesday to Thursday only, never in frost, and never within six months of the burial while the ground settles."},
                {"kind": "process", "text": "New memorials take eight to ten weeks from permit approval; additional inscriptions three to four weeks."},
                {"kind": "money", "text": "A 50% deposit at design sign-off, the balance on installation, by bank transfer against an invoice."},
                {"kind": "rule", "text": "Work within 25 miles of Frome; surveys free within 15 miles, £40 beyond."},
                {"kind": "judgement", "text": "Listed-building restoration needs listed building consent — the customer or their architect obtains it, not the firm."},
            ],
        },
        "journey": {
            "intro": "The run we proved on the live system, in order. Each step happened as described.",
            "steps": [
                {"who": "Customer", "what": "Emails to ask about a headstone for her mother, buried at St Mary's churchyard. Slate if possible; wording undecided; \"we've never had to arrange anything like this before\"."},
                {"who": "BLAM8", "what": "Drafts a reply that opens with condolences and no exclamation marks, explains the consultation-first process, reasons that a churchyard needs a church faculty rather than a cemetery permit, quotes lawn memorials from £895, and deliberately offers no dates to a bereaved first contact. The owner approves it."},
                {"who": "Customer", "what": "Replies with the inscription wording and photographs of the plot attached, asking for a consultation next Tuesday or Wednesday afternoon."},
                {"who": "BLAM8", "what": "Keys the inscription verbatim, records the photos as the required document without claiming to have read them, and holds three afternoon slots exclusively for 24 hours."},
                {"who": "Customer", "what": "\"Tuesday 8 September at 15:30, please.\""},
                {"who": "BLAM8", "what": "Flips that slot to a confirmed booking, releases the other two, and confirms in one sentence — the reply is the confirmation. The event lands in the calendar and the customer gets a reference code and a portal link in the footer."},
                {"who": "Customer", "what": "Opens the portal, asks \"is there anything you still need from me?\", asks what adding a name to her father's existing stone would cost, and asks for someone to look at the plot before the meeting."},
                {"who": "BLAM8", "what": "Answers from the promise ledger (nothing owed by her; two things owed by the firm), works the inscription price out at £110 plus 40 letters at £3.40 = £246 with the faculty caveat, offers survey windows, and books the survey — address reused from her record, documents asked for by email."},
                {"who": "Owner", "what": "Sends a quote for the memorial with an optional line for gilding the lettering and a 50% deposit."},
                {"who": "Customer", "what": "Opens the link, unticks the gilding — the total updates — and accepts by typing her name."},
                {"who": "BLAM8", "what": "Freezes the accepted version, raises the 50% deposit invoice and emails it as a PDF with the firm's bank details. Money shows the balance still to invoice, and the deal, promises and timeline are all on the customer's card without anyone typing."},
            ],
        },
        "gaps": [
            {"text": "The permit document is asked for on every visit type, including a consultation where it cannot exist yet — per-service requirement scoping is on the roadmap.",
             "built": ("Every booking requirement can now be scoped to particular services (\"only for: "
                       "Installation day\"), and the setup assistant recommends the scoping itself. The "
                       "plumber's previous gas safety record is asked only on a gas safety job; the "
                       "farrier's passport only on the first visit and the vet's practice only for "
                       "remedial work — proven in both later runs."),
             "built_on": "4 September 2026"},
        ],
    },
    {
        "slug": "kennels",
        "trade": "Kennels, dog daycare & boarding",
        "title": "How a kennels' BLAM8 is set up",
        "business": "Willowbrook Kennels",
        "business_blurb": ("A fictional dog daycare and boarding kennels: daycare days, overnight "
                           "and holiday boarding, trial nights and a meet-and-greet before any "
                           "first stay, with the dogs' details remembered per owner."),
        "industry": "kennels",
        "proven": "2–3 September 2026",
        "run_doc": "docs/e2e-tenant-checklist.md",
        "hook": "Stays that span nights, vaccination records as documents, and two dogs' details remembered under one owner.",
        # The film (docs/video-run-kennels-2026-09-20.md): a second fictional
        # kennels, Little Paws, filmed on the live system on 20 September 2026 —
        # one enquiry from the owner's seat, the waits cut, nothing staged. The
        # chapter times are the cut's cue sheet.
        "video": {
            "business": "Little Paws Daycare & Boarding",
            "filmed": "2026-09-20",
            "run_doc": "docs/video-run-kennels-2026-09-20.md",
            "src": "/static/video/setups/kennels-desktop.mp4",
            "poster": "/static/video/setups/kennels-desktop-poster.jpg",
            "seconds": 94,
            "summary": ("One enquiry for a weekend's boarding, watched from the owner's seat: the reply "
                        "already drafted with the trial night and the price, approved in a tap; the "
                        "details come back with the vaccination card and both stays are held, then "
                        "confirmed; the diary, the dog's record, the deposit out and paid, and Today "
                        "already knowing what's coming."),
            "chapters": [
                {"t": 1, "text": "An enquiry lands, the reply already written"},
                {"t": 20, "text": "Read it, tap Approve"},
                {"t": 27, "text": "The details come back, both stays held"},
                {"t": 39, "text": "Approve again, two stays confirmed"},
                {"t": 48, "text": "The diary, the dog's record and card"},
                {"t": 61, "text": "The deposit out and paid"},
                {"t": 84, "text": "Today already knows"},
            ],
            "phone": {
                "src": "/static/video/setups/kennels-phone.mp4",
                "poster": "/static/video/setups/kennels-phone-poster.jpg",
                "seconds": 74,
                "summary": ("The same day from the owner's phone: Today, the thread and its history "
                            "round by round, the diary, the dog's record with the vaccination card, "
                            "the customer's card and Money."),
            },
        },
        "diary": {
            "shape": "Stays",
            "shape_why": ("A booking occupies the kennels continuously between a drop-off and a "
                          "collection, possibly across nights, so the diary runs as stays with "
                          "capacity as how many dogs at once: 20 at a time, open 07:00–18:45 "
                          "seven days, 15-minute turnaround, 14 hours' notice, bookable a year "
                          "ahead, with weekly, fortnightly and monthly repeats (six-weekly and "
                          "quarterly joined the list after the farrier run)."),
            "services": [
                {"name": "Daycare day", "minutes": 600, "timing": "time",
                 "note": "£28 a day, £18 for a half day collected by 11:30; a same-day stay is exempt from the overnight rules."},
                {"name": "Boarding", "minutes": 1440, "timing": "day",
                 "note": "£38 a night including daycare, drop-off to collection across as many nights as booked."},
                {"name": "Trial night", "minutes": 1440, "timing": "day",
                 "note": "£30, after the meet and greet."},
                {"name": "Meet & greet", "minutes": 30, "timing": "time",
                 "note": "Free, and required before any first stay — the agent enforces it."},
            ],
        },
        "requirements": {
            "intro": ("An eleven-field checklist in the kennels' own words, with the vaccination "
                      "record asked for as an attachment. The ones that matter most:"),
            "fields": [
                {"label": "Pet name(s) & breed", "required": True, "kind": "answer",
                 "note": "Remembered on the owner's record for every future stay."},
                {"label": "Number & size of dogs", "required": True, "kind": "answer"},
                {"label": "Vaccination records", "required": True, "kind": "document",
                 "note": "A photo or scan attached to the email; the filename is recorded, the contents are never claimed as checked."},
                {"label": "Overnight stay — collection date & time", "required": True, "kind": "toggle",
                 "note": "The stay toggle: an overnight booking asks for the collection as well as the drop-off."},
                {"label": "Neutered or spayed", "required": False, "kind": "answer"},
                {"label": "Dog temperament", "required": False, "kind": "answer"},
                {"label": "Vet practice & contact number", "required": False, "kind": "answer"},
                {"label": "Medical conditions or medication", "required": False, "kind": "answer"},
            ],
        },
        "crm": {
            "stages": ["New enquiry", "Meet & greet booked", "Regular client", "On hold", "Not proceeding"],
            "quiet_days": 5,
            "fields": [
                {"label": "Dog names & breeds", "why": "Both dogs under the one owner, reused by the drafter and the portal."},
                {"label": "Vaccination status", "why": "With the date, so the kennel-cough rule can be checked."},
                {"label": "Dog temperament", "why": "Read out of the owner's own description."},
                {"label": "Vet practice & contact", "why": "Filled from a later email, resolved a promise as it arrived."},
                {"label": "Neutered / spayed", "why": "Asked once."},
                {"label": "Medical conditions", "why": "Carried into every booking's details."},
            ],
        },
        "knowledge": {
            "intro": "Five notes at setup, added to as the run found gaps. The agent applied every rule below without being prompted.",
            "rules": [
                {"kind": "rule", "text": "Every first stay is preceded by a free meet and greet."},
                {"kind": "rule", "text": "Kennel-cough vaccination at least 14 days before a stay — the agent refused to book until the jab date was given, and applied the rule in the passing direction when it was."},
                {"kind": "rate", "text": "Daycare £28 a day, £18 a half day; boarding £38 a night including daycare; trial night £30; two dogs sharing a kennel £58; bank holidays £10 extra."},
                {"kind": "rule", "text": "Half-day daycare is collected by 11:30, not 1pm — negotiated in the portal chat when a customer asked for 1pm."},
                {"kind": "money", "text": "Invoice at the end of each stay."},
            ],
        },
        # What the Little Paws rig has proven since the run and the film — each
        # line is a row in docs/verified-behaviours.md (Kris 2026-09-25: "update
        # the Kennels How it's built page"). Nothing here that isn't on that list.
        "since": [
            {"on": "22 September 2026",
             "text": "It never books a date nobody chose. Every held date is one the customer named or the diary offered, and a drop-off or collection time is the customer's own, never a guess."},
            {"on": "22 September 2026",
             "text": "A full date is offered, not booked: the nearest opening is held for the customer and named in the reply, and their yes turns it into the booking in place."},
            {"on": "22 September 2026",
             "text": "An attached vaccination card is read, filed against the right dog, and its date recorded as the day it lapses — and the jab has to cover the whole stay, not just the drop-off. The reply waits until the card is read."},
            {"on": "24 September 2026",
             "text": "\"A meet and greet and then a trial night\" in one email becomes two bookings: a 30-minute appointment and a one-night stay, each with its own reference, confirmed by one approval."},
            {"on": "24 September 2026",
             "text": "A day you've closed the diary takes no drop-off, collection or daycare. The reply says you're closed that day — never \"fully booked\" — and offers the nearest open days either side."},
            {"on": "24 September 2026",
             "text": "Getting paid: the draft shows the deposit invoice that will go with the confirmation before you approve it, and the balance — the price less the deposit — can be drafted for you the day before collection."},
            {"on": "23–24 September 2026",
             "text": "Ask the Concierge out loud (Pro + Concierge plan): which dogs are in on a given day, whose vaccinations run out soon, which invoices are overdue; keep a dog one more night and move it back — each change a card you confirm, the extra night's invoice drafted and cancelled with it."},
            {"on": "24 September 2026",
             "text": "Your team: \"Put Em on the next meet and greet\" by voice puts her on the job, and it shows on her own diary. A member of staff only sees — and can only ask for — what their role allows."},
            {"on": "25 September 2026",
             "text": "A morning brief on Today (Pro + Concierge plan): one tap opens the Concierge with the brief as her latest message, and you choose what it tells you."},
        ],
        "journey": {
            "intro": "Run five of the playbook, on a clean slate, with a new customer and two dogs.",
            "steps": [
                {"who": "Customer", "what": "Emails to ask about a trial night and a weekend's boarding for two cocker spaniels."},
                {"who": "BLAM8", "what": "Drafts a reply that enforces the meet-and-greet rule, quotes the trial night at £30 and the two-dogs-sharing rate at £58 from the knowledge base, says honestly that no dates are reserved yet, and asks only for the three details not already in the email. The customer's record is created with both dogs' names and temperament read from the enquiry."},
                {"who": "Customer", "what": "Sends the details with the vaccination record attached as a PDF."},
                {"who": "BLAM8", "what": "Records the PDF as the vaccination document, applies the 14-day kennel-cough rule, holds the trial night and the boarding weekend as two real diary rows, fills all six remembered fields on the record, and resolves the four promises the customer had just kept."},
                {"who": "Owner", "what": "Approves the reply. Both stays are confirmed, calendar events pushed, a reference code minted into the footer."},
                {"who": "Customer", "what": "Opens the portal with the reference and asks for a daycare day."},
                {"who": "BLAM8", "what": "Books it from one message with no questions — everything needed is already on the record — and emails a confirmation."},
                {"who": "Owner", "what": "Asks the Concierge to draft an invoice for the stay; confirms the card; the PDF with bank details goes out. Sends a quote with an optional extra; the customer unticks it and accepts online."},
            ],
        },
        "gaps": [
            {"text": "Today's booking details are one set per booking, not one per dog — two dogs' facts travel as one entry. Repeatable per-pet groups are on the roadmap.",
             "built": ("Dependants. You name the kind — dogs — and what to know about each one: breed asked once, "
                       "feeding notes confirmed each stay, the vaccination expiry a date that holds a booking back "
                       "when it has lapsed. Each dog is its own record on the owner's card, read from their emails; "
                       "a stay names the dogs it's for and a lapsed jab blocks that dog, not the other."),
             "built_on": "4 September 2026"},
            {"text": "The vaccination record was recorded by its filename; its contents were never claimed as checked.",
             "built": ("A certificate sent as a PDF is now read: labelled as the vaccination record, filed on the "
                       "right dog, and its expiry date taken. The expiry has to cover the whole stay, to the last "
                       "night, not just the drop-off, and a stay it doesn't cover is refused and explained, never "
                       "booked round. A new fact fills itself in; a changed one asks the owner first. A photographed "
                       "card isn't read yet."),
             "built_on": "22 September 2026"},
        ],
        # Questions a kennels owner asks before trying it. Plain text, each
        # answer fair to the other product (reports/competitors-2026-09.md §4).
        "faq": [
            {"q": "How does BLAM8 compare to kennel booking software such as Revelation Pets or KennelBooker?",
             "a": ("They're built to take bookings through a form or a customer portal, and they do it well, "
                   "with vaccination tracking of their own. Revelation Pets is £50 to £62 a month and "
                   "KennelBooker starts at $49.99 a month (prices checked 25 September 2026). BLAM8 works on "
                   "the emails that don't fit a form: the owner asking about two dogs, a trial night and a "
                   "weekend in one message. It answers in your voice from your prices and rules, gathers what's "
                   "missing, and books the stay when you approve, checking the vaccination runs to the last night. You can "
                   "run it alongside booking software you already use.")},
        ],
    },
{
        "slug": "farriers",
        "trade": "Farriers",
        "title": "How a farrier's BLAM8 is set up",
        "business": "Tanner & Vale Farriery",
        "business_blurb": "A fictional two-farrier partnership in Wells, Somerset, working livery and private yards across the Mendips: shoeing, trims, foal trims and remedial shoeing with the vet. Every visit happens at the customer's yard, and every customer has more than one horse.",
        "industry": "other",
        "proven": "4 September 2026",
        "run_doc": "docs/e2e-run-farrier-2026-09-04.md",
        "hook": "Yard visits by the half-day, a vet-first rule the agent enforces unprompted, and two horses on one booking.",
        "diary": {
            "shape": "Services",
            "shape_why": "A visit is one of a short menu of jobs of known length, so the diary runs per service. Two farriers usually work separate yards, so two visits can run at once (capacity 2), with a 30-minute drive between yards, 48 hours' notice and three months' horizon; Saturday is a morning only. The setup assistant proposed all of it from three questions, and told the customer a morning or an afternoon for every job at the yard — the owner manages the running order.",
            "services": [
                {"name": "Full set of shoes", "minutes": 60, "timing": "half", "note": "£95."},
                {"name": "Fronts only", "minutes": 30, "timing": "half", "note": "£70."},
                {
                    "name": "Trim",
                    "minutes": 30,
                    "timing": "half",
                    "note": "£35, or £30 each when three or more horses are trimmed at one yard on the same day.",
                },
                {
                    "name": "Foal trim",
                    "minutes": 30,
                    "timing": "half",
                    "note": "£25. Under-twos are trimmed, never shod.",
                },
                {
                    "name": "New-client assessment",
                    "minutes": 45,
                    "timing": "time",
                    "note": "Every new client starts here: feet and conformation of every horse before any shoeing. The one exact-time visit.",
                },
                {
                    "name": "Remedial/therapeutic shoeing",
                    "minutes": 90,
                    "timing": "half",
                    "note": "From £140, quoted once the vet's notes are in — never booked without them.",
                },
            ],
        },
        "requirements": {
            "intro": "Fourteen details, nine of them recommended by the setup assistant in the trade's own words and scoped per job — a vet question is only asked for remedial work, a horse history only on the first visit — plus the owner's own: current shoeing, handling notes, who holds the horse, and the passport as a document for the assessment only.",
            "fields": [
                {
                    "label": "Horse name",
                    "required": True,
                    "kind": "answer",
                    "note": "One per horse: each is its own record on the owner's card, named on the booking.",
                },
                {
                    "label": "Yard name and address",
                    "required": True,
                    "kind": "answer",
                    "note": "Where the horses are kept — remembered on the customer record.",
                },
                {"label": "Contact number", "required": True, "kind": "answer"},
                {
                    "label": "Is the horse currently lame?",
                    "required": True,
                    "kind": "answer",
                    "note": "The assistant's own idea: a yes triggers the vet-first rule.",
                },
                {
                    "label": "Current shoeing",
                    "required": True,
                    "kind": "answer",
                    "note": "Shod all round, fronts only, or barefoot.",
                },
                {
                    "label": "Horse age, breed and any known hoof or lameness history",
                    "required": False,
                    "kind": "answer",
                    "note": "Only asked for a new-client assessment.",
                },
                {
                    "label": "Vet name and practice",
                    "required": True,
                    "kind": "answer",
                    "note": "Only asked for remedial shoeing — remembered.",
                },
                {
                    "label": "Has the vet been consulted about this shoeing?",
                    "required": True,
                    "kind": "answer",
                    "note": "Only for remedial shoeing; answered by the attached report.",
                },
                {
                    "label": "Passport and vaccination record",
                    "required": False,
                    "kind": "document",
                    "note": "Only for the assessment, as an attachment — satisfied by a photo in the reply.",
                },
                {
                    "label": "Temperament and handling notes",
                    "required": False,
                    "kind": "answer",
                    "note": "Kicks, funny about the back feet, needs holding — remembered.",
                },
                {
                    "label": "Who will hold the horse and yard access",
                    "required": False,
                    "kind": "answer",
                    "note": "Gate codes, loose dogs, who's there.",
                },
                {
                    "label": "Last shod or trimmed date",
                    "required": False,
                    "kind": "answer",
                    "note": "Drives the six-to-eight-week cycle.",
                },
            ],
        },
        "crm": {
            "stages": ["New enquiry", "Assessment booked", "Active client", "Remedial case", "Inactive"],
            "quiet_days": 21,
            "fields": [
                {"label": "Yard address", "why": "Where the horses live — reused for every visit."},
                {"label": "Horses kept", "why": "Each horse's name, age, type and whether it is shod."},
                {"label": "Temperament notes", "why": "So the farrier knows before the horse does."},
                {"label": "Vet practice", "why": "Remedial work goes through them first."},
            ],
        },
        "knowledge": {
            "intro": "Eight notes. In the run the agent applied the assessment-first process and the vet-first rule without being asked, quoted the prices rather than deferring them, and one price note was reworded from the draft itself when it invented a 'regular customers' condition.",
            "rules": [
                {
                    "kind": "rate",
                    "text": "Full set of shoes £95; fronts only £70; trim £35 (£30 each when three or more horses are trimmed at the same yard on the same day); remedial or therapeutic shoeing from £140, quoted once we have the vet's notes; foal trim £25. Travel included within 20 miles of Wells, £1 a mile beyond.",
                },
                {
                    "kind": "judgement",
                    "text": "A lame horse sees the vet before the farrier. No remedial work is booked without the vet's notes or a referral.",
                },
                {
                    "kind": "process",
                    "text": "New clients get a yard visit and assessment first: every horse, feet and conformation, before the first shoeing.",
                },
                {
                    "kind": "process",
                    "text": "Shoes every six to eight weeks, trims every eight to ten; the next visit is booked before leaving the yard and a reminder goes a week before it's due.",
                },
                {"kind": "rule", "text": "Horses under two years old are trimmed, never shod."},
                {
                    "kind": "money",
                    "text": "Payment on the day by card or transfer; livery yards on account with one monthly invoice on 14-day terms.",
                },
                {
                    "kind": "rule",
                    "text": "A horse must be held by a competent adult; never tied up unattended, and the farrier will leave a yard where that isn't possible.",
                },
                {
                    "kind": "rule",
                    "text": "The Mendips — Wells, Shepton Mallet, Glastonbury, Frome and the villages between; not Bristol or Bath.",
                },
            ],
        },
        "journey": {
            "intro": "The run on the live system on 4 September 2026, in order.",
            "steps": [
                {
                    "who": "Customer",
                    "what": "Emails as a new client: two horses just moved to a livery yard near Wells — a twelve-year-old cob shod all round and due, and a six-year-old mare who is barefoot and needs a trim. Any afternoon this week or next. What are your prices and what do you need from us?",
                },
                {
                    "who": "BLAM8",
                    "what": "Explains the assessment-first rule unprompted, quotes £95 and £35 with travel included, checks the diary and asks only for what the checklist still lacks — a contact number and whether either horse is lame — plus a preferred afternoon. Both horses land on the record from the one email.",
                },
                {
                    "who": "Owner",
                    "what": "Flags one line — the £30 trim rate had grown a 'regular customers' condition the notes never said. The assistant finds the note, rewrites it in place from the chat, and the draft regenerates with three afternoon slots reserved.",
                },
                {
                    "who": "Customer",
                    "what": "Monday afternoon please — with a phone number, 'neither is lame, though Murphy's a bit funny about his back feet', the gate code, a loose collie, and the passports attached as a photo.",
                },
                {
                    "who": "BLAM8",
                    "what": "Books Monday on the first try with every detail keyed under the business's own labels, the passport satisfied by the attachment without claiming to have read it. The reply is the confirmation: 'You're booked in for Monday 7 September, afternoon'.",
                },
                {
                    "who": "Customer",
                    "what": "Adds: Murphy has been a bit short on his near fore — could you look at remedial shoes on the same visit?",
                },
                {
                    "who": "BLAM8",
                    "what": "Applies the vet-first rule: nothing remedial is booked, the vet is asked to see him first, the assessment stands.",
                },
                {
                    "who": "Customer",
                    "what": "Sends the vet's report from Mendip Equine — bar shoes on the fronts, can you book that in?",
                },
                {
                    "who": "BLAM8",
                    "what": "Offers three half-day windows for remedial shoeing with the vet practice keyed, the price 'from £140, confirmed once the notes are reviewed', and the report received rather than read.",
                },
                {
                    "who": "Customer",
                    "what": "Opens the portal with the reference from the email footer: anything still needed? Then picks Monday afternoon for the remedial work, and asks for both horses' next visit about six weeks on, any morning.",
                },
                {
                    "who": "BLAM8",
                    "what": "Answers from the promise ledger, books the remedial slot into the same afternoon as the assessment with a confirmation email, and books the October visit for both horses with no questions — everything was on file.",
                },
                {
                    "who": "Owner",
                    "what": "Sends a quote for the remedial shoeing with optional pads and a 25% deposit; the customer unticks the pads and accepts by typing their name.",
                },
                {
                    "who": "BLAM8",
                    "what": "Raises the 25% deposit invoice and emails it with the bank details within seconds. Money shows the balance still to invoice, net of the deposit.",
                },
            ],
        },
        "gaps": [
            {
                "text": "Two horses at one yard visit had to share one booking: every per-horse detail became a 'Murphy; Tilly' string, and the portal refused the second horse's trim as a duplicate of the first.",
                "built": "Dependants: you name what your bookings are about — horses — and what to know about each one: asked once, confirmed each time, or an expiry that can hold a booking back. Every horse is its own record on the owner's card, filled from their emails and chats; a booking names the horses it's for.",
                "built_on": "4 September 2026",
            },
            {
                "text": "Recurring bookings offered weekly, fortnightly or monthly; a farrier's cycle is every six to eight weeks.",
                "built": "Standing bookings repeat every six weeks or once a quarter as well.",
                "built_on": "4 September 2026",
            },
            {
                "text": "Asked how much trims would be for three horses at the yard, the portal said it had no pricing — the price note was there.",
                "built": "A short question finds the note that holds the words: 'trim price?' → £35, and £30 each for three or more at one yard.",
                "built_on": "4 September 2026",
            },
            {
                "text": "The page a customer lands on to pay the deposit was a bare block of bank details.",
                "built": "It reads like the quote page it follows: your header, the lines, the total, the due date, and the bank details with the reference in bold.",
                "built_on": "4 September 2026",
            },
            {
                "text": "The passport and the vet's report were read from the email — the expiry date lands on the horse's record — but the files themselves were not kept.",
                "built": "Documents on the record: a file that comes with an email is kept on the owner's card, labelled as the checklist document it satisfies, pinned to the horse or the booking, open in a click.",
                "built_on": "4 September 2026",
            },
        ],
    },
{
        "slug": "hot-tub-servicing",
        "trade": "Hot tub servicing",
        "title": "How a hot tub servicing firm's BLAM8 is set up",
        "business": "Dovedale Hot Tub Services",
        "business_blurb": "A fictional two-van firm in Ashbourne, Derbyshire, looking after hot tubs at holiday cottages and private homes across the Peak District: quarterly services, drain-clean-and-refill, HSG282 microbiological testing for lets, repairs and commissioning. Every visit happens at the tub, and a holiday-let owner often has one tub per cottage.",
        "industry": "trades",
        "proven": "4 September 2026",
        "run_doc": "docs/e2e-run-hottubs-2026-09-04.md",
        "hook": "Each tub its own record with a test certificate that can hold a booking back, a guest-rash rule the agent enforces unprompted, and quarterly repeats.",
        "diary": {
            "shape": "Services",
            "shape_why": "A visit is one of six jobs of known length at the customer's tub, so the diary runs per service. Two vans are out at once (capacity 2), with half an hour's driving between jobs, 48 hours' notice and three months' horizon; Saturday is a morning only. The setup assistant proposed all of it from three questions, told customers a morning or an afternoon for every visit, and picked the quarterly and six-weekly repeats for the service cycle and the busy-let refills.",
            "services": [
                {
                    "name": "Routine service",
                    "minutes": 60,
                    "timing": "half",
                    "note": "£95 per tub, £85 each when two or more tubs at one site are done on the same visit.",
                },
                {
                    "name": "Drain, clean and refill",
                    "minutes": 150,
                    "timing": "half",
                    "note": "£145 including chemicals.",
                },
                {
                    "name": "HSG282 water test",
                    "minutes": 30,
                    "timing": "half",
                    "note": "£65 per tub plus £30 lab fee per sample; every quarter for a holiday let.",
                },
                {
                    "name": "Repair call-out",
                    "minutes": 90,
                    "timing": "half",
                    "note": "£85 for the first hour then £45 per half hour; parts always quoted after seeing the tub.",
                },
                {"name": "New tub commissioning", "minutes": 180, "timing": "half", "note": "£250."},
                {
                    "name": "Chemical starter pack delivery",
                    "minutes": 15,
                    "timing": "half",
                    "note": "£60, delivered on the round.",
                },
            ],
        },
        "requirements": {
            "intro": "Twelve details, nine of them recommended by the setup assistant in the trade's own words and scoped per job — a parts deposit only for a repair call-out — plus the owner's own: the last lab certificate as a document for the water test only, the fault code for repairs only, and when the next guests arrive. The tubs themselves are not on this list: each is a dependant record on the owner's card.",
            "fields": [
                {
                    "label": "Site address",
                    "required": True,
                    "kind": "answer",
                    "note": "The cottage or house the tubs are at — remembered on the customer record.",
                },
                {"label": "Contact name", "required": True, "kind": "answer"},
                {"label": "Contact phone number", "required": True, "kind": "answer"},
                {
                    "label": "Property type",
                    "required": True,
                    "kind": "answer",
                    "note": "Private home or holiday let — lets come under HSG282.",
                },
                {
                    "label": "Isolator accessible?",
                    "required": True,
                    "kind": "answer",
                    "note": "The isolator and RCD must be reachable before the visit.",
                },
                {
                    "label": "Someone on site to let us in?",
                    "required": True,
                    "kind": "answer",
                    "note": "The owner, a housekeeper or a key safe — remembered as the access contact.",
                },
                {
                    "label": "Access instructions",
                    "required": False,
                    "kind": "answer",
                    "note": "Gate codes, key safe, parking.",
                },
                {
                    "label": "Next guests arrive",
                    "required": False,
                    "kind": "answer",
                    "note": "Only for a service, a refill or a test — so the tub is guest-ready in time.",
                },
                {
                    "label": "Fault or error code showing",
                    "required": True,
                    "kind": "answer",
                    "note": "Only asked for a repair call-out.",
                },
                {
                    "label": "Last lab certificate (microbiological)",
                    "required": False,
                    "kind": "document",
                    "note": "Only for the water test, as an attachment — received, never claimed as read.",
                },
                {
                    "label": "Parts deposit paid?",
                    "required": False,
                    "kind": "answer",
                    "note": "The assistant's own idea, repair call-outs only: 50% on parts over £300.",
                },
                {
                    "label": "Number of tubs on site",
                    "required": False,
                    "kind": "answer",
                    "note": "For the multi-tub rate; redundant once every tub is a dependant.",
                },
            ],
        },
        "crm": {
            "stages": ["New enquiry", "Quoted / awaiting approval", "Booked in", "Active contract", "Gone cold"],
            "quiet_days": 21,
            "fields": [
                {"label": "Property address", "why": "Where the tubs are — reused for every visit."},
                {"label": "Access contact", "why": "The housekeeper or changeover company who opens up."},
                {"label": "Water source", "why": "Mains, borehole or spring changes the chemistry."},
                {"label": "Account terms", "why": "On the day, or a monthly account on 14-day terms."},
            ],
        },
        "knowledge": {
            "intro": "Eleven notes, written by the setup assistant from a chat interview — it asked for prices, coverage, hours, the first visit, the repeat cycle, payment and safety in the trade's own terms, and wrote each as a note the owner saved with a click. In the run the agent quoted the multi-tub rate, applied the guest-rash rule and the empty-tub rule without being asked, and did the year's arithmetic on the portal.",
            "rules": [
                {
                    "kind": "rate",
                    "text": "Routine service £95 per tub, £85 each for two or more tubs at one site on the same visit; drain, clean and refill £145 with chemicals; microbiological water test £65 per tub plus a £30 lab fee per sample; repair call-out £85 for the first hour then £45 per half hour, parts quoted first; commissioning £250; chemical starter pack £60. Travel included within 15 miles of Ashbourne, £1 a mile beyond.",
                },
                {
                    "kind": "judgement",
                    "text": "Guests or family reporting a rash, sore eyes or illness after using a tub takes the tub out of use: drain, disinfect, refill and a microbiological sample before anyone uses it again — never just extra chemicals.",
                },
                {
                    "kind": "rule",
                    "text": "A holiday-let tub needs an in-date quarterly microbiological test certificate before it's signed off as guest-ready.",
                },
                {
                    "kind": "rule",
                    "text": "A tub that has stood empty for more than a week gets a refill and 24 hours of heating before it can be serviced or tested.",
                },
                {
                    "kind": "process",
                    "text": "A new customer's first visit combines a routine service with a water test: data plate photographed, isolator and RCD checked, water tested, log book set up — an HSG282 log book for lets.",
                },
                {
                    "kind": "process",
                    "text": "Routine service every quarter; holiday-let tubs tested every quarter with the certificate expiry tracked; drain and refill every three months for a private tub, every six weeks for a busy let; the next visit booked before leaving and a reminder a week ahead.",
                },
                {
                    "kind": "money",
                    "text": "Payment on the day by card or transfer; holiday-let owners on a monthly account with 14-day terms; repairs with parts over £300 need a 50% deposit on the parts.",
                },
                {
                    "kind": "rule",
                    "text": "The isolator and RCD must be reachable and are switched off before work; no work on a tub with people in it; the job is left in a lightning storm; chemicals locked away from children and pets.",
                },
                {
                    "kind": "rule",
                    "text": "The Peak District — Ashbourne, Bakewell, Matlock, Wirksworth, Buxton, Leek and the villages between; not Sheffield, Derby or Stoke city centres.",
                },
            ],
        },
        "journey": {
            "intro": "The run on the live system on 4 September 2026, in order.",
            "steps": [
                {
                    "who": "Customer",
                    "what": "Emails as a new holiday-let owner: two cottages at Thorpe with a tub each — a Hot Spring Envoy and 'a smaller Caldera, I'd have to check' — changeovers on Fridays, a full service on both and whatever testing lets need. What does it cost and what do you need from us? Any weekday morning.",
                },
                {
                    "who": "BLAM8",
                    "what": "Quotes the two-tub rate and the test price with travel included, recommends the quarterly cycle and six-weekly refills unprompted, and asks only for what's missing: a phone number, isolator access, who lets you in, the Caldera's model, when the next guests arrive — and invites the last lab certificate as an attachment, 'helpful but not a blocker'. The Byre's tub lands on the owner's card from the one email.",
                },
                {
                    "who": "Customer",
                    "what": "Tuesday morning please — a phone number, 'a Vacanza I think, or maybe the Kauai', the isolators in the meter cupboards, Jo the housekeeper who lets you in, next guests on the 18th, and the Byre's certificate attached as a photo.",
                },
                {
                    "who": "BLAM8",
                    "what": "Acknowledges the certificate without claiming to have read it, records Jo as the access contact, and asks for the Dairy's model and certificate before booking.",
                },
                {
                    "who": "Customer",
                    "what": "The Dairy is a Vacanza and has no certificate. And last week's guests at the Byre came out in a rash — can you just put some extra chlorine in on Tuesday?",
                },
                {
                    "who": "BLAM8",
                    "what": "Refuses the chlorine and applies the rule: the Byre comes out of use, drain, disinfect, refill and a fresh sample; the Dairy gets its test. Holds Tuesday morning on the first try as one visit about both tubs — each its own record, no 'A; B' strings — and says 'we've held Tuesday 8th September morning for you'.",
                },
                {
                    "who": "Customer",
                    "what": "Confirms Tuesday, gives the Byre's certificate date, and says: bill everything to me — Jo just needs the times.",
                },
                {
                    "who": "BLAM8",
                    "what": "Turns the held slot into the booking on the first call; the reply is the confirmation. The Byre's certificate expiry is written to its record from the sentence.",
                },
                {
                    "who": "Customer",
                    "what": "Mentions in passing that the Dairy's certificate runs to December after all, and a third cottage — the Granary — came with a Jacuzzi nobody has tested, let from October.",
                },
                {
                    "who": "BLAM8",
                    "what": "Writes the Dairy's new expiry to its record, books the Granary's test onto the same Tuesday morning as a third tub, and flags the empty-tub rule: it may need a refill and 24 hours' heating before it can be tested.",
                },
                {
                    "who": "Customer",
                    "what": "Opens the portal with the reference from the email footer: anything still needed? What would quarterly servicing for all three tubs cost a year? Book the Dairy's routine service for 6 October. Can you add the Byre's test to that visit? The Dairy's water was changed on the 19th.",
                },
                {
                    "who": "BLAM8",
                    "what": "Answers from the promise ledger, does the arithmetic (3 × £85 × 4 = £1,020) and offers a standing quarterly booking, books 6 October with no questions and emails the confirmation, updates the Dairy's record from the chat, and adds the Byre's test to that visit as a second tub on the one booking.",
                },
                {
                    "who": "Owner",
                    "what": "Sends a quote for a replacement circulation pump with an optional ozone unit and a 25% deposit; the customer unticks the ozone unit and accepts by typing their name.",
                },
                {
                    "who": "BLAM8",
                    "what": "Raises the £60 deposit invoice and emails it within seconds; Money shows the balance still to invoice, net of the deposit.",
                },
            ],
        },
        "gaps": [
            {
                "text": "Told to add a second tub's test to a visit already in the diary, the portal said it had — the diary had refused it as a duplicate, and there was no way to add a tub or a second service to an existing booking.",
                "built": "A visit about a different tub on the same morning is not a duplicate; the portal adds another tub or a second service to a visit the customer already has (one booking, one reference); a reply can no longer claim a change the diary refused.",
                "built_on": "4 September 2026",
            },
            {
                "text": "The certificate rule was applied a step too early: a holiday-let tub with no certificate yet can't be signed off as guest-ready, but its first visit is the test itself and should book. The owner corrected the note from the draft itself.",
            },
            {
                "text": "'Bill everything to me — Jo just needs the times' names an on-site contact and a payer; Jo landed on the record as the access contact but not as a party on the booking.",
                "built": "A person named on a visit is a party of that visit — bound to the visit on the day it speaks of, their details from wherever in the conversation they were given. The customer is never a party of their own booking, and a booking added by hand says which tubs it is about.",
                "built_on": "4 September 2026",
            },
            {
                "text": "The lab certificate that came by email was a filename on the booking; the file itself lived only in the email.",
                "built": "Documents on the record: a file that comes with a customer's email is kept on their card, labelled as the checklist document it satisfies, pinned to a tub or a booking, open in a click — and the agent never asks for a document already on file. Uploads by hand too.",
                "built_on": "4 September 2026",
            },
        ],
    },
{
        "slug": "mot-garage",
        "trade": "MOT and service garages",
        "title": "How an independent MOT and service garage's BLAM8 is set up",
        "business": "Foxholes Garage",
        "business_blurb": "A fictional independent garage in Ludlow, Shropshire: three ramps, one of them the class 4 MOT bay, two technicians and a service adviser on the desk. MOTs, interim and full services, diagnostics, brakes and tyres on cars and small vans; two courtesy cars; collection and delivery within ten miles. Customers come to the garage, many own more than one car, and a company-car driver's bill goes to the lease company.",
        "industry": "other",
        "proven": "4 September 2026",
        "run_doc": "docs/e2e-run-garage-2026-09-04.md",
        "hook": "Each car its own record with an MOT date that reminds rather than refuses, a ramp and an MOT bay as pools, a lease company as the payer, and the DVSA's own rules answered unprompted.",
        "diary": {
            "shape": "Services",
            "shape_why": "The work happens at the garage, so the diary runs per service with the drop-off shape a customer actually experiences: an MOT is a 45-minute waiting slot at a clock time on the single MOT bay, a service is a leave-it-with-us day on one of two ramps. The setup assistant proposed the hours (Saturday mornings for MOTs only), 24 hours' notice, the seven services with prices, the two resource pools — one Class 4 MOT bay, two general ramps, never double-booked — and collection and delivery within ten miles at £15. Each service is linked to its pool, and the horizon runs 60 days for the plate-change months.",
            "services": [
                {
                    "name": "MOT",
                    "minutes": 45,
                    "timing": "time",
                    "note": "£45 — the legal maximum is £54.85. A waiting appointment on the MOT bay.",
                },
                {"name": "Interim Service", "minutes": 120, "timing": "day", "note": "£120, leave it with us."},
                {
                    "name": "Full Service",
                    "minutes": 180,
                    "timing": "day",
                    "note": "£199 up to 2.0 litres, from £249 above; £99 electric.",
                },
                {"name": "MOT and Interim Service", "minutes": 240, "timing": "day", "note": "£165 together."},
                {"name": "Diagnostics", "minutes": 60, "timing": "time", "note": "£60 for the first hour."},
                {
                    "name": "Brakes",
                    "minutes": 60,
                    "timing": "day",
                    "note": "Never priced before the car has been seen — quoted first, then the go-ahead.",
                },
                {"name": "Tyres", "minutes": 30, "timing": "time", "note": "Quoted by size."},
            ],
        },
        "requirements": {
            "intro": "Seven details, four recommended by the setup assistant in the garage's own words and three added by the owner — the last service stamp as a photo for services only, a yes or no to a road test for diagnostics and repairs, and the courtesy car. The cars themselves are not on this list: each is a dependant record on the customer's card, with its registration, make, fuel, a note of where the locking wheel nut key lives, and an MOT date that expires without ever blocking a booking.",
            "fields": [
                {
                    "label": "Reason for visit / what the car is doing",
                    "required": True,
                    "kind": "answer",
                    "note": "Symptoms, warning lights, noises — briefs the technician before the car arrives.",
                },
                {
                    "label": "Drop-off or collection and delivery",
                    "required": True,
                    "kind": "answer",
                    "note": "Drop it off from 8, or collected and returned within 10 miles for £15.",
                },
                {
                    "label": "Approximate mileage",
                    "required": False,
                    "kind": "answer",
                    "note": "'60-odd thousand' is enough — keyed as a number, never re-asked.",
                },
                {
                    "label": "Work authoriser and PO or fleet reference",
                    "required": False,
                    "kind": "answer",
                    "note": "Services and repairs only: who signs off extra work on a company car.",
                },
                {
                    "label": "Photo of the last service stamp or digital service record",
                    "required": False,
                    "kind": "document",
                    "note": "Services only, as an attachment — kept on the record, never claimed as read.",
                },
                {
                    "label": "OK for us to road-test it?",
                    "required": False,
                    "kind": "answer",
                    "note": "Diagnostics, brakes and tyres only — some faults only show on the road.",
                },
                {
                    "label": "Courtesy car needed?",
                    "required": False,
                    "kind": "answer",
                    "note": "Two cars, first come; £10 a day, free with a full service.",
                },
            ],
        },
        "crm": {
            "stages": [
                "New enquiry",
                "Booked in",
                "Car with us",
                "Awaiting authorisation",
                "Awaiting parts",
                "Ready to collect",
                "Job done",
                "Lost",
                "Account / fleet",
            ],
            "quiet_days": 5,
            "fields": [
                {
                    "label": "Who pays / account",
                    "why": "The customer, their employer or the lease company — filled from the first email.",
                },
                {
                    "label": "Authorisation reference",
                    "why": "The PO or fleet reference the payer wants quoted before work starts.",
                },
                {
                    "label": "Preferred contact",
                    "why": "How they want to hear the car is ready or that work needs authorising.",
                },
            ],
        },
        "knowledge": {
            "intro": "Eight notes, one per kind of fact, written by the setup assistant from a chat interview that opened with its own three questions in the garage's terms. In the run the agent did the DVSA arithmetic on the early-MOT window, quoted every price, asked the lease company question before the visit, and told a customer with a spongy brake pedal not to drive the car in — all unprompted.",
            "rules": [
                {
                    "kind": "rate",
                    "text": "MOT £45 (the legal maximum is £54.85). Interim service £120; full service £199 up to 2.0 litres, from £249 above, electric £99; MOT and interim service together £165; diagnostics £60 for the first hour; labour £75 an hour; collection and delivery £15 within 10 miles of Ludlow, free with a full service; courtesy car £10 a day, free with a full service. All prices include VAT.",
                },
                {
                    "kind": "judgement",
                    "text": "Nothing beyond the booked work is started without the customer's say-so: the price first, then the yes, and the invoice matches what was agreed. A car that fails its MOT with a dangerous fault cannot be driven away until it is repaired — said before the test, never after.",
                },
                {
                    "kind": "judgement",
                    "text": "Brakes, steering or a red warning light reported means: don't drive it, we'll collect it — never 'pop it in when you can'.",
                },
                {
                    "kind": "rule",
                    "text": "An MOT can be done up to a month minus a day before it runs out and keeps the same renewal date — the customer is always told when that window opens.",
                },
                {
                    "kind": "process",
                    "text": "New customers give the registration, the approximate mileage and what the car is doing; the service book or digital record is photographed at the first visit and the MOT and service dates noted for reminders. Company cars: who authorises work and any PO or fleet reference, before the visit.",
                },
                {
                    "kind": "process",
                    "text": "MOT yearly; full service every 12 months or 12,000 miles; interim at 6 months or 6,000 miles for high mileage; a reminder a month before the MOT is due. Replaced parts are kept for the customer to see; parts and labour guaranteed 12 months; the customer is updated during the day and told when the car is ready; no road test without permission.",
                },
                {
                    "kind": "money",
                    "text": "Payment on collection by card or bank transfer (cashless); lease companies and account customers get an itemised invoice on 30-day terms; parts over £250 need a 50% deposit before they are ordered.",
                },
                {
                    "kind": "rule",
                    "text": "Class 4 cars and small vans only — no motorbikes, no vans over 3,000 kg, no motorhomes. Monday to Friday 8am to 5:30pm, Saturday 8am to 1pm for MOTs; 24 hours' notice; March and September MOT slots go a fortnight ahead.",
                },
            ],
        },
        "journey": {
            "intro": "The run on the live system on 4 September 2026, in order.",
            "steps": [
                {
                    "who": "Customer",
                    "what": "Emails: the Golf's MOT runs out on 28 September and it's due a service — is it worth testing early or do I lose the days? My partner Sam's Fiesta needs an MOT too, I'll dig out the reg. What do you charge, can we drop both on the same morning, and the Golf is a company car through Fleetline who pay for servicing.",
                },
                {
                    "who": "BLAM8",
                    "what": "Answers the DVSA rule with the dates worked out — 'your early-booking window opened on 29 August… the new certificate runs from 28 September regardless' — quotes the prices, explains the Golf is a leave-it-with-us day while the Fiesta's MOT is a 45-minute waiting slot, asks who authorises work for Fleetline and whether they use a PO, and asks only what's missing per car, including a photo of the last service stamp as an attachment. Fleetline lands on the customer's card as who pays.",
                },
                {
                    "who": "Customer",
                    "what": "Sends the service stamp photo, then the details: YK19 HDF, a 2019 2.0 TDI on 60-odd thousand, MOT and interim service; Fleetline's fleet desk address and a PO to follow; Sam's Fiesta MK17 LPQ, petrol, MOT due 14 October; Tuesday 15 September for both, Golf dropped at 8 for the day, Sam waiting with the Fiesta; wheel nut key in the glovebox.",
                },
                {
                    "who": "BLAM8",
                    "what": "Keeps the photo on the customer's record and says so. Books the Golf's MOT and interim service for the 15th — every detail keyed by its label, the photo satisfying the document — with the Golf as its own vehicle record (registration, model, fuel, where the wheel nut key lives), and binds Fleetline's fleet desk to the job as the payer who approves work.",
                },
                {
                    "who": "Customer",
                    "what": "Tuesday 15th for both please — and since yesterday the Golf's brake pedal has gone spongy with a grinding noise from the front. Still OK to drive it in?",
                },
                {
                    "who": "BLAM8",
                    "what": "'Please do not drive the Golf in… we'd strongly recommend letting us collect it… once we've inspected the brakes we'll give you a full quote before touching anything.' Holds BOTH cars on the 15th in one reply — the Golf for the day, the Fiesta as a 45-minute waiting appointment at 8am — each on its own vehicle record; the brake symptom is written to the Golf's record as a known issue.",
                },
                {
                    "who": "Customer",
                    "what": "Collect it tomorrow then — key with reception at the business park. Fleetline's PO is FL-88213, invoices to their fleet desk. Sam is still fine for the 15th.",
                },
                {
                    "who": "BLAM8",
                    "what": "Says honestly that tomorrow is inside the 24 hours' notice and offers Monday, Tuesday or Wednesday for the collection, held until the next evening; restates the quote-first rule and that the customer will be kept updated and told when the car is ready; puts the collection charge on Fleetline's invoice under the PO.",
                },
                {"who": "Customer", "what": "Monday 7 September please — the key will be with reception from 8."},
                {
                    "who": "BLAM8",
                    "what": "Turns the offered Monday into the booking and releases the other two days. Approved: the Golf is confirmed for collection on the 7th with a calendar event, the Fiesta's MOT stands on the 15th, and the Golf's 15th slot is cancelled from the diary by hand.",
                },
                {
                    "who": "Customer",
                    "what": "Opens the portal with the reference from the email footer, greeted by name with the Monday collection.",
                },
            ],
        },
        "gaps": [
            {
                "text": "Two cars, one morning: the diary took one hold per email, so the reply told the customer 'could you send a separate email for the Fiesta'.",
                "built": "One hold per visit — a second vehicle on the same email is its own hold, up to three.",
                "built_on": "4 September 2026",
            },
            {
                "text": "The MOT date on each car must not block a booking — a lapsed MOT is the reason to book — so nothing reminded anyone.",
                "built": "An expiry that doesn't block is a recall: when it is due within a month or has lapsed and nothing is booked for that car, Today carries the card in the owner's words.",
                "built_on": "4 September 2026",
            },
            {
                "text": "A redraft asked for after skipping a draft twice came back empty where a booking reply belonged, with no reason given.",
                "built": "A redraft that comes back empty is filed as a failed draft with the reason on the thread — write the reply or redraft.",
                "built_on": "5 September 2026",
            },
            {
                "text": "The PO number the customer sent was not written over the record's earlier 'not yet obtained', so the next reply asked for it again. A placeholder on the record should give way to the real value.",
            },
            {
                "text": "Moving the Golf's job from the 15th to the collection day left the 15th confirmed too — a reply that moved a job could not release the old slot.",
                "built": "The agent moves the customer's own booking from their email: in time it moves in place and keeps its reference; inside the notice rule the old booking is cancelled as a late cancellation and the new time held for approval, or the move is free — the business's setting. A whole series is still rearranged by the business.",
                "built_on": "6 September 2026",
            },
            {
                "text": "The customer's day booking and a 45-minute MOT sat on the same one-bay pool for the same morning — a whole-day booking on a one-count pool should take the pool for the day.",
            },
        ],
    },
{
        "slug": "swim-school",
        "trade": "Children's swim schools",
        "title": "How a children's swim school's BLAM8 is set up",
        "business": "Ripple Swim School",
        "business_blurb": "A fictional independent swim school in Ledbury, Herefordshire, hiring the pool at a hotel leisure club: four teachers, 30-minute classes of up to six children by Swim England stage, parent-and-baby Ducklings, private one-to-ones, Saturday and Sunday mornings plus weekday after-school sessions, and termly enrolment — the same slot every week for twelve weeks, paid in advance, with half-term off. A family has several children with the school, each at a different stage, and a grandparent often brings them.",
        "industry": "other",
        "proven": "5 September 2026",
        "run_doc": "docs/e2e-run-swim-school-2026-09-05.md",
        "hook": "Each child its own record with a stage the teacher moves, classes with six places a slot, a term booked as a weekly series that skips half-term, a grandparent poolside as a named party, and the 48-hour sickness rule worked out on the real dates.",
        "diary": {
            "shape": "Slots",
            "shape_why": "Classes run at the pool's fixed times, so the diary is 30-minute slots with six places in each — a class is a slot with a capacity, not a job with a length. From one chat message the setup assistant proposed the slot length, six places per slot, a different opening span for every day (Saturday 8–1, Sunday 9–12, Monday to Thursday 3:30–6:30, Friday closed), 24 hours' notice, a 120-day horizon so a term ahead is bookable, the four services, and weekly recurring bookings up to twelve times — the term.",
            "services": [
                {
                    "name": "Assessment swim",
                    "minutes": 15,
                    "timing": "time",
                    "note": "Free, before any child is placed; every new child has one.",
                },
                {
                    "name": "Group lesson",
                    "minutes": 30,
                    "timing": "time",
                    "note": "£9 a class, £108 for a twelve-week term; six to a class, eight for Stages 4–7; 10% off a second child.",
                },
                {
                    "name": "Ducklings",
                    "minutes": 30,
                    "timing": "time",
                    "note": "Parent and baby, 6 months to 3 years; £10 a class, £120 a term; swim nappies under three.",
                },
                {"name": "Private 1:1", "minutes": 30, "timing": "time", "note": "£28 per half hour."},
            ],
        },
        "requirements": {
            "intro": "Nine details: the ones the setup assistant recommended, minus a parent's email address and the child's medical facts (those live on the child's record), plus the school's own asks — the signed consent and medical form as a photo attachment for everything but a private lesson, who brings the child, and the placement question every teacher asks. The children are not on this list: each is a dependant record on the parent's card, with a date of birth, medical conditions, a stage the teacher moves, and whether a swim nappy and a photo consent apply.",
            "fields": [
                {
                    "label": "Which day and time works",
                    "required": True,
                    "kind": "answer",
                    "note": "Saturday or Sunday morning, or a weekday after school — the same slot every week for the term.",
                },
                {
                    "label": "Parent or guardian's phone number",
                    "required": True,
                    "kind": "answer",
                    "note": "For urgent poolside contact.",
                },
                {
                    "label": "Emergency contact name and phone number",
                    "required": True,
                    "kind": "answer",
                    "note": "A second adult to call if the parent is not reachable; asked once, then on the record.",
                },
                {
                    "label": "Signed consent and medical form",
                    "required": False,
                    "kind": "document",
                    "note": "A photo or scan, as an attachment; kept on the record and never re-asked. Not for private lessons.",
                },
                {
                    "label": "Who brings your child to lessons",
                    "required": False,
                    "kind": "answer",
                    "note": "Parent, grandparent or childminder — they stay poolside; a named one becomes a party on the booking.",
                },
                {
                    "label": "Can your child put their face in the water and blow bubbles?",
                    "required": False,
                    "kind": "answer",
                    "note": "The placement question; assessment swims and group lessons only.",
                },
                {
                    "label": "Previous swim school and badge or stage reached",
                    "required": False,
                    "kind": "answer",
                    "note": "Background for the teacher only — the assessment decides placement, never the badge.",
                },
                {
                    "label": "What would you like to focus on in the lesson",
                    "required": False,
                    "kind": "answer",
                    "note": "Private lessons only.",
                },
                {
                    "label": "Anything else we should know",
                    "required": False,
                    "kind": "answer",
                    "note": "Nerves, a previous bad experience, anything that helps the teacher.",
                },
            ],
        },
        "crm": {
            "stages": [
                "New enquiry",
                "Assessment booked",
                "Assessment done",
                "Form outstanding",
                "Term enrolled",
                "Waiting list",
                "Not proceeding",
                "Left",
            ],
            "quiet_days": 7,
            "fields": [
                {
                    "label": "Emergency contact",
                    "why": "The second adult on the day, filled from the first email that names one.",
                },
                {
                    "label": "Who pays",
                    "why": "A grandparent, an ex-partner or an employer when it isn't the parent writing.",
                },
                {
                    "label": "Preferred contact",
                    "why": "Email, text or phone — and when (evenings, not the school run).",
                },
            ],
        },
        "knowledge": {
            "intro": "Eight notes, one per kind of fact, written by the setup assistant from a chat interview in the school's terms. In the run the agent refused to place a child from a badge, quoted the term price with the sibling discount worked out, asked for the consent form as an attachment, applied the 48-hour rule to the actual dates, and told a grandparent she must stay poolside — all unprompted.",
            "rules": [
                {
                    "kind": "rate",
                    "text": "£9 per 30-minute class, booked and paid by the term — £108 for a twelve-week term per child; Ducklings £10 a class, £120 a term; private one-to-ones £28 per half hour; second and further children 10% off their term fee; the assessment swim is free.",
                },
                {
                    "kind": "judgement",
                    "text": "A child who has had sickness or diarrhoea stays out of the pool until 48 hours after the last episode; after a confirmed cryptosporidium infection, two weeks (PWTAG's own words). The dates are worked out in the reply, against the booking on the record.",
                },
                {
                    "kind": "rule",
                    "text": "No photography or filming poolside or in the changing rooms; badge photos only with the signed consent form; phones stay in bags in the changing rooms (Swim England's Wavepower). A parent or named adult stays poolside for every class for children under eight.",
                },
                {
                    "kind": "process",
                    "text": "Every new child has a free 15-minute assessment swim before placement — water confidence and the Swim England stage they are working at — and is never booked straight into a stage from a parent's description or a badge from elsewhere. The signed consent and medical form comes before the first lesson.",
                },
                {
                    "kind": "process",
                    "text": "Classes run weekly, the same day and time all term; the autumn term is twelve weeks with no lessons in half-term week. Current swimmers get first refusal on their slot for the next term before the waiting list is offered places; a child moves up a stage when the teacher signs off the outcomes, at any point in the term.",
                },
                {
                    "kind": "money",
                    "text": "Term fees within 7 days of the place being confirmed, by bank transfer or card; monthly standing orders by arrangement; one make-up lesson per term for a class missed with 24 hours' notice, subject to space, no refunds otherwise; pro-rata for a child joining mid-term.",
                },
                {
                    "kind": "rule",
                    "text": "Teachers hold Swim England Level 2 or the STA equivalent, a current DBS check and safeguarding training; a lifeguard is poolside at every session; classes are six for Stages 1–3 and eight for Stages 4–7; no adult other than a teacher in the water except in Ducklings.",
                },
                {
                    "kind": "rule",
                    "text": "One pool, in Ledbury; families from Ledbury, Colwall, Bromyard, Newent and the villages between. Saturday 8am to 1pm, Sunday 9am to noon, Monday to Thursday 3:30 to 6:30pm, no classes on Fridays; 24 hours' notice for an assessment; September fills first and the spring waiting list opens in November.",
                },
            ],
        },
        "journey": {
            "intro": "The run on the live system on 5 September 2026, in order.",
            "steps": [
                {
                    "who": "Customer",
                    "what": "Emails: Saturday morning lessons for my two — Isla is 5 and has never had lessons, nervous of water on her face; Finn is 8, swims ten metres and has a Stage 3 badge from a school in Gloucester. Can Finn go straight into Stage 3? What does the term cost and what do you need from us?",
                },
                {
                    "who": "BLAM8",
                    "what": "Says no to Stage 3 from a badge — every new child has the free assessment first — quotes £108 a term and works out Finn's £97.20 with the sibling discount, notes Isla's nerves for the teacher, and asks only what is missing per child: dates of birth, medical conditions for each, a phone and emergency contact, a day that works, and the signed consent form as a photo attachment. The reply carries the customer's reference and portal link.",
                },
                {
                    "who": "Customer",
                    "what": "Sends the signed form as a photo, then the details: both dates of birth, Isla's asthma with the inhaler in her bag, Finn has no medical conditions, Nana Pat brings them on alternate Saturdays, an assessment on Saturday 12th at 8:30 please.",
                },
                {
                    "who": "BLAM8",
                    "what": "Keeps the form on the customer's record and says so. Holds BOTH assessments at 8:30 on the 12th in one reply — two places of the six in that slot — each on its own child record (date of birth, medical conditions, the swim-nappy and photo-consent facts derived), with Finn's 'no medical conditions' recorded in the parent's words, and binds Pat Deane to both bookings as the adult on site. Tells the parent the inhaler goes poolside and that Pat is welcome as the adult who stays.",
                },
                {"who": "Customer", "what": "Finn was sick on Thursday night — is he still OK for Saturday morning?"},
                {
                    "who": "BLAM8",
                    "what": "'If Finn was sick on Thursday night (3 September), the 48-hour period ends on Saturday evening, 5 September. His assessment is on Saturday 12 September at 08:30, well clear of that — so he is fine to attend. If he has another episode, the 48-hour rule resets.' The dates are written out against the booking on the record.",
                },
                {
                    "who": "Customer",
                    "what": "Can you move both assessments to Sunday 13th at 9:30? And, after the assessments, hold Isla a Stage 1 place at 9:30 on Saturdays for the term and Finn a Stage 3 place at 10:00.",
                },
                {
                    "who": "BLAM8",
                    "what": "Says plainly that placement follows the assessment and holds nothing yet (the school's own rule), asking only for the emergency contact's number. The move to Sunday it cannot make by email — that goes to the school.",
                },
                {
                    "who": "Owner",
                    "what": "After the assessments, enrols both children from the diary: Group lesson, Saturday 19 September, 9:30 and 10:00, Repeat weekly, twelve times, About Isla / Finn — 24 confirmed lessons in two series, the half-term Saturday skipped and the term extended to 12 December, every one on the calendar and on the child's record.",
                },
                {
                    "who": "Customer",
                    "what": "Opens the portal with the reference from the email: asks what is still needed (both sides of the ledger), what the spring term would cost (the sum with the sibling discount), whether Isla can move to 10:30 (a series move goes to the school — offered, not faked), books Finn a private lesson on Sunday 20th from one line, and asks what stage Finn is in — 'According to the record, Stage 4', the fact the teacher had just written.",
                },
                {
                    "who": "Owner",
                    "what": "Asks the concierge to invoice the term — £108 and £97.20, due in 7 days — confirms the card, then confirms the send: the PDF with the school's bank details lands in the parent's inbox and Money shows £205.20 owed.",
                },
            ],
        },
        "gaps": [
            {
                "text": "Recurring was on and the agent could hold a series, but the diary's own Add form had no repeat control — the school could not enrol a child for the term after the assessment, its everyday move.",
                "built": "Repeat and Times on the Add form: each date placed like a single add, half-term and closed days skipped and the term extended, every lesson on the calendar and on the child's record.",
                "built_on": "5 September 2026",
            },
            {
                "text": "A make-up lesson, a waiting list with first refusal, and a 'moved up a stage' message are the incumbents' table stakes; the agent stated each rule honestly and handed off.",
                "built": "Built the same day and proven on a driving school: an excused miss with notice mints a make-up to book within a window; a full slot is joined as a waiting list from the portal and the freed place is offered by the morning sweep; moving a child's stage drafts a message to the parent for approval.",
                "built_on": "5 September 2026",
            },
            {
                "text": "'Finn has no medical conditions' was keyed as 'None', refused by the booking gate as a placeholder, and the reply asked the parent to say it again.",
                "built": "A genuine none is recorded in the parent's words; the next hold went through first time.",
                "built_on": "5 September 2026",
            },
            {
                "text": "The 48-hour sickness arithmetic was right and applied to the wrong Saturday, three drafts running — the booking on the 12th was on the record.",
                "built": "The drafter writes the dates inside the reply and concludes from the dated booking on the record.",
                "built_on": "5 September 2026",
            },
            {
                "text": "The email agent could not move a confirmed booking — 'move both assessments to Sunday' became 'a colleague will sort that out'.",
                "built": "The agent moves the customer's own booking from their email: in time it moves in place and keeps its reference; inside the notice rule the old booking is cancelled as a late cancellation and the new time held for approval, or the move is free — the business's setting. A whole series is still rearranged by the business.",
                "built_on": "6 September 2026",
            },
            {
                "text": "After the setup steps the owner is told in prose to go through the inbox before switching drafting on; it needs to be a step with a button.",
            },
        ],
    },
    {
        "slug": "driving-instructor",
        "trade": "Driving instructors",
        "title": "How a driving instructor's BLAM8 is set up",
        "business": "Hartley Driving School",
        "business_blurb": ("A fictional DVSA-approved driving instructor in Ledbury, Herefordshire: one instructor, one "
                           "dual-control manual car, pupils collected from home within ten miles, lessons of one or two "
                           "hours, a two-hour assessment lesson first, a ten-hour block at a discount, an intensive "
                           "twenty-hour course, a mock test, and the car for the driving test itself — with the hour "
                           "before. Monday to Saturday; Saturday mornings fill first and have a waiting list. The pupil "
                           "is the customer: there is no child or animal or vehicle in between."),
        "industry": "other",
        "proven": "5 September 2026",
        "run_doc": "docs/e2e-run-driving-2026-09-06.md",
        "hook": "Blocks sold in hours and spent by the lesson's length, a progress ladder in the DVSA's own words on the pupil's record with the test-day car gated on 'Test ready', the theory certificate's expiry answered from the record, a lesson cancelled from the pupil's own email under the 48-hour rule, and a Saturday waiting list joined from the portal.",
        "diary": {
            "shape": "Services",
            "shape_why": ("Every booking is one of a menu of fixed-length services on a 60-minute grid with one car, so the "
                          "diary is services with a capacity of one: Monday to Friday 8 to 7, Saturday 8 to 2, 48 hours' "
                          "notice, a 15-minute gap between lessons for the pick-ups, a standing weekly slot held as one "
                          "recurring booking, the Saturday waiting list on, and the business's own words — 'lesson' and "
                          "'pupil'. The cancellation rule is the diary's own: 48 hours, and a lesson missed or cancelled "
                          "late spends its hours; a lesson moved inside the 48 hours counts as a late cancel and a "
                          "fresh booking — the school's own setting, it could be free. The setup assistant proposed "
                          "all of it from one message."),
            "services": [
                {"name": "1-hour lesson", "minutes": 60, "timing": "time", "note": "£34; spends one hour of a block."},
                {"name": "2-hour lesson", "minutes": 120, "timing": "time", "note": "£66; spends two hours of a block."},
                {"name": "Assessment lesson", "minutes": 120, "timing": "time", "note": "£66; every new pupil's first lesson — eyesight check, controls, a drive against the DVSA driver's record."},
                {"name": "Mock test", "minutes": 90, "timing": "time", "note": "£50; not part of a block."},
                {"name": "Test-day car hire", "minutes": 180, "timing": "time", "note": "£110; the car for the test with the hour before — only for a pupil marked 'Test ready'."},
            ],
        },
        "requirements": {
            "no_document": ("Nothing to attach: the provisional licence is shown at every lesson, never sent, so the "
                            "checklist confirms it each time instead of asking for a copy."),
            "intro": ("Five details in the instructor's words. Facts about the pupil — the licence number, the glasses "
                      "— live on the pupil's record and are asked once; a booking asks only what changes each time: "
                      "where to collect you, and whether the licence is with you."),
            "fields": [
                {"label": "Pick-up address", "required": True, "kind": "answer",
                 "note": "Where we collect you — home, work or college; asked once, then filled from the record."},
                {"label": "Provisional licence with you", "required": True, "kind": "answer",
                 "note": "A legal requirement — no licence, no lesson. Lessons, the assessment and the mock test."},
                {"label": "Glasses or contact lenses for driving", "required": False, "kind": "answer",
                 "note": "The eyesight rule: a number plate from 20 metres, with glasses or lenses if needed."},
                {"label": "What you want to work on", "required": False, "kind": "answer",
                 "note": "Roundabouts, the dual carriageway, manoeuvres, the test route. Lessons only."},
                {"label": "Test date and centre", "required": False, "kind": "answer",
                 "note": "Hereford or Worcester. Mock tests and the test-day car only."},
            ],
        },
        "crm": {
            "stages": ["Enquiry", "Assessment booked", "Active pupil", "Block purchased", "Test ready", "Passed",
                       "On hold", "Withdrawn"],
            "quiet_days": 21,
            "fields": [
                {"label": "DVSA driver's record", "why": "The progress ladder on the pupil's record — Introduced, Helped, Prompted, Independent, Reflection, Test ready. The instructor moves it from the pupil's card; a move drafts a message to the pupil for approval; on the portal it reads 'Level on the DVSA driver's record: Prompted — 3 of 6, next Independent'; the test-day car is refused, by email and on the portal, until 'Test ready'."},
                {"label": "Theory test pass date", "why": "The date on the certificate, valid for two years from it and recalled 60 days before it lapses — the pupil asks when it runs out and the portal answers from the record."},
                {"label": "Test date", "why": "The booked practical test, recalled 30 days before so the car can be planned. '14 December' in an email lands on the next 14 December."},
                {"label": "Test centre", "why": "Hereford or Worcester, filled from the pupil's first mention."},
                {"label": "Provisional licence number", "why": "Asked once, on file for the DVSA paperwork."},
                {"label": "Glasses or contact lenses needed", "why": "The eyesight rule, asked once."},
                {"label": "Pick-up address", "why": "So a returning pupil is never asked again."},
            ],
        },
        "knowledge": {
            "intro": ("Eight notes, one per kind of fact, written by the setup assistant from the owner's own answers "
                      "in one message. In the run the agent put the assessment lesson first, quoted every price, stated "
                      "the 48-hour rule and the eyesight rule before the first booking, and held a two-hour assessment "
                      "and then a ten-week Saturday series from one line each."),
            "rules": [
                {"kind": "rate", "text": "£34 an hour or £66 for two hours; a block of ten hours £320, valid six months; an intensive twenty-hour course £620 within three months; a mock test (90 minutes) £50; the car for the driving test with the hour before, £110. Pay per lesson by bank transfer or card, or a block in advance."},
                {"kind": "rule", "text": "Bring your provisional licence to every lesson — without it we cannot drive. You must be able to read a number plate from 20 metres, with glasses or lenses if you need them (the DVSA standard). Moving the driving test itself needs 10 working days' notice with the DVSA or the fee is lost."},
                {"kind": "judgement", "text": "A lesson cancelled with less than 48 hours' notice, or missed, is charged in full or spends an hour of the block — worked out against the lesson's real date. Lessons run in all weather except ice and fog. The car is not available for a test unless the instructor has seen the pupil drive to test standard."},
                {"kind": "process", "text": "Every pupil starts with a two-hour assessment lesson: eyesight check, controls, and a drive to place them on the DVSA driver's record (27 skills, from introduced to independent); then the hours to the test are planned. Beginners usually need 40–45 hours with an instructor plus private practice; a pupil who has had lessons before, fewer."},
                {"kind": "process", "text": "Most pupils have one or two lessons a week, the same day and time, held as a standing weekly booking; Saturday mornings fill first and carry a waiting list."},
                {"kind": "money", "text": "A ten-hour block is spent by the lesson's length — a two-hour lesson is two hours off the block; blocks are not refunded once started; unused hours can be transferred to another pupil by arrangement; late cancellations and no-shows spend an hour, an excused miss with notice gives it back."},
                {"kind": "rule", "text": "A DVSA-approved driving instructor (green badge, grade A), fully insured, a dual-control car serviced every 6,000 miles, a DBS check on file; pupils collected and returned to the agreed address."},
                {"kind": "rule", "text": "Ledbury and within about ten miles — Colwall, Bromyard, Newent, Much Marcle, Dymock; test centres Hereford and Worcester. Monday to Friday 8am to 7pm, Saturday 8am to 2pm, no Sundays; two weeks off at Christmas."},
            ],
        },
        "journey": {
            "intro": "The run on the live system, 5–6 September 2026, in order.",
            "steps": [
                {"who": "Customer", "what": "Emails: six lessons with an instructor who moved away, theory passed in March, roundabouts and the dual carriageway scare me; Saturday mornings if you have them — what does it cost, is there a block deal?"},
                {"who": "BLAM8", "what": "Puts the two-hour assessment lesson first, quotes every price, states the 48-hour rule, the licence and the eyesight rule, says Saturday mornings have space and asks for the pick-up address before anything is held."},
                {"who": "Customer", "what": "The address, the licence, no glasses, the theory pass date, Saturday 10:00 for two hours please — and yes to the ten-hour block."},
                {"who": "BLAM8", "what": "Holds the assessment at Saturday 10:00 with the details keyed exactly, puts the theory pass date on the pupil's record with the date it runs out, and confirms with the reference and the block's terms. Approved: on the calendar, in the pupil's inbox."},
                {"who": "Owner", "what": "Asks the assistant to invoice the block, sends it, marks it paid: the wallet holds ten hours, Money shows £320 collected."},
                {"who": "BLAM8", "what": "'Every Saturday at 10 for the next ten weeks?' — one hold with a weekly repeat of ten, each Saturday checked, all ten dates in the reply. The morning after each lesson its hours come off the block without a register tap."},
                {"who": "Customer", "what": "On the portal: anything you still need from me (everything is on file); how many hours do I have left (nine of ten, valid until 4 March 2027); a note on the lesson; when does my theory certificate run out — answered from the record."},
                {"who": "Owner", "what": "Moves the pupil to 'Prompted' on the DVSA driver's record from the pupil's card; a message to the pupil — 'you've moved up to Prompted' — lands in the approval queue and goes out."},
                {"who": "BLAM8", "what": "The car for the test on 14 December, asked by email and on the portal — refused both times: 'It requires you to be at Test ready on your DVSA driver's record, and you're currently at Prompted.' A test date months ahead is 'not open to book yet — opens on…', never 'fully booked'."},
                {"who": "BLAM8", "what": "'Can't make Monday's lesson — work', sent inside 48 hours: the lesson is cancelled from the email, the hour spent under the rule, and the reply states the rule and the outcome. An excused miss told three days ahead gives its hour back and mints a make-up."},
                {"who": "Owner", "what": "Cancels the standing series by hand; a short note to the pupil lands in the approval queue and every freed Saturday goes to the waiting list. A second pupil who had joined it from the portal is offered the Saturday and books it."},
                {"who": "Owner", "what": "With two hours left on the block and its expiry ten days out, the 'nearly used up' and 'running out soon' notes are composed once each for approval; with the theory certificate 45 days from lapsing and nothing booked, a recall card appears on Today."},
            ],
        },
        "gaps": [
            {"text": "Every class-business feature assumed a dependant between the customer and the booking — a child, a dog, a car. With the pupil as the customer, the register and the block's balance were hidden on the lesson page, the sweep never spent an hour, the instructor could not move a pupil's level, and the test-day gate never read the pupil's own record.",
             "built": "All of it works on the pupil's own record: register, balance and notes on the lesson page; hours spent the morning after; the level moved from the pupil's card with a message written to the pupil; the test-day car refused by email and on the portal until 'Test ready'.", "built_on": "5 September 2026"},
            {"text": "The email agent could not cancel a lesson: 'can't make Monday' drew replies stating the 48-hour rule and leaving the diary change to a colleague.",
             "built": "The agent cancels the pupil's own booking from their email, spends or refunds the hour by the 48-hour rule with the email's time as the notice, frees the place for the waiting list, and the reply states the outcome. A move from the pupil's email works the same way: in time the lesson moves in place and keeps its reference; inside the 48 hours it counts as a late cancel plus a fresh booking, or moves free — the school's own setting.", "built_on": "6 September 2026"},
            {"text": "A theory pass date on the record read as 'lapsed' the day it was recorded, because an expiring field treated its date as the lapse date.",
             "built": "An expiring field carries how long the thing is valid — a theory certificate 24 months from its date — and the record, the recall and the portal answer with the date it runs out.", "built_on": "6 September 2026"},
            {"text": "The pupil was told nothing when the owner cancelled his series by hand, and a cancelled series freed only one of its Saturdays for the waiting list.",
             "built": "A hand cancel composes a short note to the pupil for approval; a cancelled series frees every date it held.", "built_on": "6 September 2026"},
            {"text": "A test date months ahead was answered as 'fully booked' on an empty day.",
             "built": "Beyond the booking horizon the answer is 'not open to book yet — opens on…', and the setup proposes a longer horizon for a trade that books test days ahead.", "built_on": "6 September 2026"},
            {"text": "The setup cards misread a few things: the 48-hour rule left at the 24-hour default, the licence number as a per-booking row, a two-year certificate written as a 730-day reminder, a dropdown with no options dropped without a word.",
             "built": "The diary card claims the cancellation rule; a fact about the pupil goes to the record, not the booking; a reminder past a year is read as a validity; a dropdown carries its options — and the page says what landed and what was refused.", "built_on": "6 September 2026"},
        ],
    },
]

BY_SLUG = {s["slug"]: s for s in SETUPS}


def validate() -> list[str]:
    """Every entry carries every section the page renders, with sane values.
    Returns the list of problems (empty = valid); the tests assert it is empty."""
    problems: list[str] = []
    seen = set()
    for s in SETUPS:
        slug = s.get("slug") or "?"
        if slug in seen:
            problems.append(f"{slug}: duplicate slug")
        seen.add(slug)
        for key in ("trade", "title", "business", "business_blurb", "industry", "proven",
                    "run_doc", "hook", "diary", "requirements", "crm", "knowledge", "journey",
                    "gaps"):
            if not s.get(key):
                problems.append(f"{slug}: missing {key}")
        for svc in s.get("diary", {}).get("services", []):
            if svc.get("timing") not in TIMING_WORDS:
                problems.append(f"{slug}: service {svc.get('name')} bad timing")
            if not isinstance(svc.get("minutes"), int) or svc["minutes"] <= 0:
                problems.append(f"{slug}: service {svc.get('name')} bad minutes")
        fields = s.get("requirements", {}).get("fields", [])
        if not any(f.get("required") for f in fields):
            problems.append(f"{slug}: no required field")
        # A document row (a certificate, a form) or an honest reason there is
        # none — a driving pupil shows the licence on the day, nothing is sent.
        if not any(f.get("kind") == "document" for f in fields) \
                and not str(s.get("requirements", {}).get("no_document") or "").strip():
            problems.append(f"{slug}: no document field (or a no_document reason)")
        for f in fields:
            if f.get("kind") not in KINDS:
                problems.append(f"{slug}: field {f.get('label')} bad kind")
        if len(s.get("crm", {}).get("stages", [])) < 3:
            problems.append(f"{slug}: too few stages")
        if len(s.get("knowledge", {}).get("rules", [])) < 3:
            problems.append(f"{slug}: too few knowledge rules")
        if len(s.get("journey", {}).get("steps", [])) < 5:
            problems.append(f"{slug}: journey too short")
        for st in s.get("journey", {}).get("steps", []):
            if st.get("who") not in ("Customer", "BLAM8", "Owner"):
                problems.append(f"{slug}: journey step bad actor {st.get('who')}")
        # "Proven since": optional; each line dated and said in words.
        for item in s.get("since") or []:
            if not (isinstance(item, dict) and str(item.get("on") or "").strip()
                    and str(item.get("text") or "").strip()):
                problems.append(f"{slug}: a since item needs 'on' and 'text'")
        # A gap is the sentence the run wrote, or that sentence plus what was
        # built since ("built") and when ("built_on") — the page shows both, so
        # a fixed gap stays on the record as the thing the run found.
        for g in s.get("gaps", []):
            if isinstance(g, str):
                continue
            if not isinstance(g, dict) or not g.get("text"):
                problems.append(f"{slug}: gap without text")
            elif set(g) - {"text", "built", "built_on"}:
                problems.append(f"{slug}: gap has unknown keys {sorted(set(g) - {'text', 'built', 'built_on'})}")
        for qa in s.get("faq", []):
            if not isinstance(qa, dict) or not str(qa.get("q") or "").strip()                     or not str(qa.get("a") or "").strip() or set(qa) - {"q", "a"}:
                problems.append(f"{slug}: faq entries need exactly a q and an a")
        if s.get("video") is not None:
            problems.extend(f"{slug}: {p}" for p in _video_problems(s["video"]))
    return problems


def _clip_problems(clip: dict, what: str) -> list[str]:
    """One clip's mp4, poster and length, all under VIDEO_DIR."""
    problems: list[str] = []
    src, poster = clip.get("src") or "", clip.get("poster") or ""
    if not (src.startswith(VIDEO_DIR) and src.endswith(".mp4")):
        problems.append(f"{what} src must be an mp4 under {VIDEO_DIR}")
    if not (poster.startswith(VIDEO_DIR) and poster.endswith((".jpg", ".png"))):
        problems.append(f"{what} poster must be a jpg/png under {VIDEO_DIR}")
    secs = clip.get("seconds")
    if not isinstance(secs, int) or not 0 < secs <= 120:
        problems.append(f"{what} seconds must be 1–120 (the playbook says under 90 plus the end card)")
    if not str(clip.get("summary") or "").strip():
        problems.append(f"{what} needs a summary (the player's alt text and the page's description)")
    return problems


def _video_problems(v) -> list[str]:
    """The film entry: the desktop clip with its chapters, the rig it was filmed
    on and when, the run notes — and the optional phone clip beside it."""
    if not isinstance(v, dict):
        return ["video must be a dict"]
    problems: list[str] = []
    if set(v) - VIDEO_KEYS:
        problems.append(f"video has unknown keys {sorted(set(v) - VIDEO_KEYS)}")
    for key in ("business", "filmed", "run_doc"):
        if not str(v.get(key) or "").strip():
            problems.append(f"video missing {key}")
    try:
        date.fromisoformat(str(v.get("filmed") or ""))
    except ValueError:
        problems.append("video filmed must be an ISO date (YYYY-MM-DD)")
    if not str(v.get("run_doc") or "").startswith("docs/"):
        problems.append("video run_doc must be under docs/")
    problems.extend(_clip_problems(v, "video"))
    chapters = v.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        problems.append("video needs chapters (the cue sheet)")
    else:
        last = -1
        for ch in chapters:
            t = ch.get("t") if isinstance(ch, dict) else None
            if not isinstance(t, int) or t < 0 or not str(ch.get("text") or "").strip():
                problems.append(f"video chapter {ch!r} needs an integer t and a text")
                continue
            if t <= last:
                problems.append(f"video chapters must run in order ({t}s after {last}s)")
            if isinstance(v.get("seconds"), int) and t >= v["seconds"]:
                problems.append(f"video chapter at {t}s is past the end ({v['seconds']}s)")
            last = t
    phone = v.get("phone")
    if phone is not None:
        if not isinstance(phone, dict):
            problems.append("video phone must be a dict")
        else:
            if set(phone) - CLIP_KEYS:
                problems.append(f"video phone has unknown keys {sorted(set(phone) - CLIP_KEYS)}")
            problems.extend(_clip_problems(phone, "video phone"))
    return problems


def _shipped(*urls: str) -> bool:
    """Every /static/... URL given is a file on disk under STATIC_DIR."""
    return all(url.startswith("/static/") and (STATIC_DIR / url[len("/static/"):]).is_file()
               for url in urls)


def film(s: dict) -> dict | None:
    """The setup's film for the page, once its files have shipped: None until the
    desktop clip and its poster are under app/static (the clips are cut on Kris's
    machine and copied in; the page lights up on the deploy that carries them).
    The phone clip is dropped the same way when only it is missing."""
    v = s.get("video")
    if not v or not _shipped(v["src"], v["poster"]):
        return None
    out = dict(v)
    phone = v.get("phone")
    if phone and not _shipped(phone["src"], phone["poster"]):
        out.pop("phone")
    return out


def films() -> dict[str, dict]:
    """slug → film, for the setups that have one on disk (the index's chip)."""
    out = {}
    for s in SETUPS:
        f = film(s)
        if f:
            out[s["slug"]] = f
    return out


def lower_trade(text: str) -> str:
    """A trade name mid-sentence: lower case, except a word written in capitals
    (an acronym keeps them). The setup pages lower-cased the whole name, so a
    tab title lost an acronym's capitals (setup-page finding, 1 Oct 2026)."""
    return " ".join(w if (len(w) > 1 and w.isupper()) else w.lower() for w in str(text or "").split(" "))


def long_date(iso: str) -> str:
    """'2026-09-20' → '20 September 2026', the way the pages write dates."""
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B %Y')}"


def clock(seconds: int) -> str:
    """94 → '1:34', a chapter's time the way a player shows it."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


DISCLAIMER = ("The businesses on these pages are fictional — worked configurations we built "
              "to run BLAM8 end to end on the live system, not customers. Every step "
              "described happened in the run it cites.")


def duration_label(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        n = minutes // 1440
        return "overnight" if n == 1 else f"{n} nights"
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} hr" + (f" {m} min" if m else "")
