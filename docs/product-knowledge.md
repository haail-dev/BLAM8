# Product knowledge and maintenance

This is the current source and maintenance contract. Implementation in a checkout
is not proof of deployment, provider readiness or availability to a tenant.

## Sources and authority

| Concern | Owner |
|---|---|
| Capability evidence and affected surfaces | `src/app/product_inventory.py` |
| Settings definitions and supported proposal controls | `src/app/setup_catalogue.py` and existing schemas |
| Business reads and confirmed actions | `src/app/concierge_data.py`, `src/app/concierge_actions.py` |
| Access | Existing module, People, route and executor gates |
| Public prices, plans and entitlements | `src/app/plans.py`; metered allowance enforcement also lives in `src/app/usage.py` |
| Authored articles | `src/app/platform_articles.json`, stable IDs retained on rename |
| Business-neutral instructions | `src/app/product_guides.py` |
| Current help bundle and offline refresh plan | `src/app/platform_content.py` |
| Public human-facing route inventory | `src/app/pages_index.py` |
| Verification and rollout qualifications | `docs/verified-behaviours.md` |

The platform catalogue adapts these owners rather than replacing their validation
or introducing another settings writer. It receives modules and permissions;
industry names and business descriptions cannot grant capability.
Keep business vocabulary, facts and user-chosen presets in tenant context.

## Agent and public content

Public questions, scenarios and signed-in product help read the current local
bundle. The owner's typed and voice assistants have the same read-only product
tool. An exhaustive request returns the complete capability index and the actual
setup definitions, not a nearest-neighbour sample.

Availability distinguishes unknown context, permission required, module required,
preview, restricted, disabled and configuration-dependent access. Discovery never
authorises execution. Actual record reads, proposals and confirmed execution keep
their existing tenant, revision, permission and late checks.

Tenant business knowledge remains separate. Editing a Markdown file does not
update a tenant's indexed knowledge. The historical `agent-knowledge` material is
not silently loaded or re-seeded. The extractor supports Markdown uploads, but
uploading historical material does not make its claims current.

Help topics feed both visible help and JSON-LD. Public templates resolve commercial
values from the catalogue; comparison-provider claims remain editorial evidence
and must be reviewed separately. Security, contractual and residency claims require
reviewed policy/provider evidence, not an inference from an endpoint existing.

## Versions and refreshes

Bundles hash complete articles and the definition catalogue. A refresh plan names
added, changed and retired stable IDs, the expected old version and the desired
complete manifest. Renaming a title retains its ID. An incomplete, tampered, old
or foreign-tenant receipt cannot verify a complete version.

`python scripts/knowledge/seed_platform_kb.py` now prepares a JSON plan only. An optional
`--current-manifest PATH` reconciles a supplied managed-platform manifest. It never
embeds, deletes data, publishes or activates anything. The compatibility seeding
function also only prepares. Unmanaged legacy indexes require separate inspection;
do not sweep or overwrite tenant knowledge to refresh product content.

No live publication mechanism is enabled by this work. Runtime product help uses
the release's local bundle, so stale vector rows do not override current help.
The catalogue describes the Knowledge admission review already present in this
repository (`knowledge_review.py` and onboarding routes), conservatively marked
restricted because discovery does not verify its database rollout. Source-backed
corrections are now adapted to that admission pipeline, behind the disabled
`KNOWLEDGE_SOURCE_REVIEW_ENABLED` flag. The existing admission migration stays
in place; a separate source-snapshot migration and explicit release verification
are required. See [Source-backed Knowledge corrections](operations/knowledge-source-review.md)
for installation, retention and rollback. The catalogue does not claim enablement.
Website capture now inventories location-labelled non-main source wording too,
reports page/character limits and requests separate exact passages per note
clause. The owner still resolves literal coverage and exclusions. Attribution
does not prove semantic completeness, and older pending drafts are unchanged.

## Updating a feature

1. Change its existing implementation, schema or gate, with behaviour tests.
2. Update its evidence-map association and shared article/guide. Preserve IDs.
3. Review affected help, public claims, JSON-LD and current technical references.
4. Record release qualifications. Retain dated design/audit records as history.
5. Run the offline coverage checks and affected tests. Record a reason for any
   mapped surface that needs no change; do not use a blanket documentation waiver.
6. Commit the evidence, content and tests with the feature. Publication and feature
   enablement are separate release operations.

Offline checks prove deterministic context, coverage and boundary contracts.
They do not certify natural-language model answer quality or live availability.

## Change gate and remaining documentation debt

Run `python scripts/ci/audit_product_coverage.py` to check the current evidence map,
guide coverage and source-bound review receipts. Run
`python scripts/ci/check_product_change.py --base COMMIT` to additionally reject
changed runtime sources with no capability association. CI runs this gate before
the existing full offline suite. The commands make no model or database calls.

`docs/product-change-reviews.json` records the author's disposition for each
affected Agent, document and public surface, including source evidence and a
signature of the reviewed sources. It is not human approval. A changed signature,
missing surface, unexplained waiver or new unaccounted runtime source fails.
Review the actual change and update its specific dispositions; do not blindly
regenerate signatures. Semantic accuracy still requires the normal code review.

The document inventory reports maintained references, explicit history/design,
and review-required debt. Unreviewed older documents are not silently certified
as current. The existing agnostic debt ledger and real-caller snapshot gate remain
active; source moves and intended prompt changes require their own exact review.
