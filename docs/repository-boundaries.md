# Repository boundaries

This repository contains the BLAM8 platform, its build configuration, current
product and operations documentation, and ordinary offline regression tests.

The original BLAutoM8 repository and the migration reference archive preserve
historical work, research, creative production projects, live browser rigs,
screenshot capture, test runs, and shared assistant tooling.

## Excluded platform entry points

The candidate does not include the operator demo-world generator, its seed/start/
stop admin routes, the development voice-harness route, or the promotional-film
campaign queue and its media-proof/publishing support. Their navigation links and
film-only migration files are also excluded. The /rig/* and /concierge/voice/rig/*
APIs, their static rig page, and rig-only settings are excluded too.
No live database was modified.

Customer-facing email, scheduling, CRM, quoting, invoicing, Concierge, voice,
telephone, and ordinary account/tenant administration remain platform features.
Customer photo/document handling is independent of filming and screenshot tools.

## Regression tests and external QA

`tests/` retains checks of platform behaviour using synthetic fixtures, mocked
providers, disposable local databases, and JavaScript replay checks. It does not
include Playwright/browser automation or screenshot-writing test functions.

The excluded browser and filming checks remain preserved with their original
source for a separate QA/tooling workspace. A later external harness can test the
platform through its normal public interfaces without becoming an application
runtime dependency.

Source-inventory records for deleted rig/film code were removed from the agnostic
debt inventory. The scanner, existing platform behaviour fixtures, and outcome
assertions were not relaxed to admit the excluded tooling.

## Provenance and unfinished work

`baseline-provenance.json` records the pinned starting commit. Active branches,
local modifications, stashes, and historical Git objects were not imported wholesale.
The original workspace remains the reference for reviewing unfinished features.
