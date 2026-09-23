# CRM and workflow benchmark for LinkBound v3

Reviewed 2026-09-22. Closed-source CRM documentation exposes logical objects and APIs, not internal database schemas. Open-source repository models provide implementation evidence. The [architecture plan](linkbound-v3-architecture-plan.md) contains the resulting decisions.

## What existing systems actually separate

| System | Implemented distinction | Consequence for LinkBound |
| --- | --- | --- |
| [Attio](https://attio.com/help/reference/attio-101/attios-data-model/understanding-lists) | A person is a reusable record. A list entry holds process-specific fields, and the same person can have separate entries for different recruiting applications. | Keep person identity separate from campaign enrollment and candidate or client workflow state. |
| [Salesforce](https://developer.salesforce.com/docs/data/data-cloud-dmo-mapping/guide/c360dm-si-campaignmemberdmo-dmo.html) | Campaign Member links a campaign to a lead or contact and holds its own status and response date. | A campaign member is a durable entity, not a field on the contact or a send job. |
| [HubSpot](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/schemas/guide) | Records have properties and explicit associations. Activities such as notes, tasks, emails, and conversations can associate with records. [Contact-company associations](https://knowledge.hubspot.com/records/associate-records) can designate a primary company. | Model organizations and observed person affiliations explicitly. Keep timeline entries linked to their source records. |
| [EspoCRM](https://docs.espocrm.com/user-guide/campaigns/) | Campaigns use inclusion and exclusion target lists and a campaign event log. [Relationships](https://docs.espocrm.com/development/api/relationships/) are first-class API operations. | Make suppression and audience provenance explicit. Avoid flattening campaign history into a contact's latest status. |
| [Chatwoot](https://github.com/chatwoot/chatwoot/blob/develop/app/models/contact_inbox.rb) | A shared contact maps to a channel inbox through `ContactInbox`, unique on `(inbox_id, source_id)`. It owns conversations, which own [messages](https://github.com/chatwoot/chatwoot/blob/develop/app/models/message.rb) and [attachments](https://github.com/chatwoot/chatwoot/blob/develop/app/models/attachment.rb). | Preserve one person across sender accounts while storing account-specific provider identity, threads, messages, and files. |
| Mautic | [Campaign steps](https://github.com/mautic/mautic/blob/7.x/app/bundles/CampaignBundle/Entity/Event.php) and [per-contact execution logs](https://github.com/mautic/mautic/blob/7.x/app/bundles/CampaignBundle/Entity/LeadEventLog.php) are separate. Logs hold schedule, channel, result, and failure metadata, with a unique event, lead, and rotation key. | Separate campaign membership, versioned step definition, queued work, and immutable attempt outcome. |
| [Twenty](https://github.com/twentyhq/twenty) | People, companies, tasks, notes, and custom objects form a configurable CRM application. | Useful model reference. Its full application is a separate deployment and integration choice. |

These products do not document a ready-made model for multiple LinkedIn sender profiles, browser profile locking, invitation uncertainty, or LinkedIn inbox observations. Those are LinkBound-specific records and policies. A CRM owner or assignee is a human responsibility field; a LinkedIn sender account is a channel identity. This distinction is an inference from the documented models, not a quoted vendor rule.

## Revised primitive map

| Layer | Record and ownership | Invariant |
| --- | --- | --- |
| Identity | `people`, `person_identities`, `contact_points`, `organizations`, `person_affiliations` | A person can have multiple observed URLs, email addresses, and company affiliations. Unknown or conflicting identity stays unresolved; no merge by name alone. |
| Sender relationship | `accounts`, `account_contacts` | One relationship per sender account and person. Connection evidence, contact state, and last sync are account-scoped. |
| Business context | `campaigns`, `campaign_memberships`, `campaign_steps` | One person can participate in several candidate or client campaigns. Enrollment status and context are independent of the person's identity. V1 uses one membership per person per campaign. |
| Execution | `work_items`, `send_attempts`, `activity_events` | A durable due item can be leased. Every attempted browser action has an immutable result; an uncertain outcome cannot silently become a retry. |
| Inbox | `conversations`, `conversation_participants`, `messages`, `attachments` | Threads and messages belong to the sender account. Provider IDs are unique within their provider and account scope; weak fingerprints carry match confidence. |
| Human decisions | `review_items`, `tasks`, `suppression_rules`, `consent_records` | Exceptions and ordinary follow-ups are distinct. Both have a durable owner, source, status, and resolution; tasks also have a due time. |
| Intake and integration | `import_jobs`, `import_rows`, `sync_runs`, `integration_deliveries` | Original row, mapping, rejection, sync coverage, and external delivery remain traceable and idempotent. |

`activity_events` are an audit and timeline layer. A source message or send attempt remains in its own table and an event points to it; the message body is not duplicated as the canonical event payload. A reply is attached to an account contact and conversation first. Campaign attribution is made only when an outbound touch can be linked to the reply, or after review. This prevents a reply from being assigned to whichever campaign was most recently imported.

### Cardinality and idempotency checks

| Record | Constraint or rule |
| --- | --- |
| `account_contacts` | Unique `(account_id, person_id)`; a provider source ID is unique only within the account and provider scope where observed. |
| `campaign_memberships` | Unique `(campaign_id, person_id)` in the first version. A new role or outreach motion uses a new campaign, while the person record stays shared. |
| `work_items` | Unique `(membership_id, step_id)` for a step version. Claims use a transaction, lease token, and conditional update; attempts are separate append-only rows. |
| `messages` | Unique `(account_id, provider_message_id)` when a stable ID exists. A fallback fingerprint is a candidate match with confidence; a repeated identical message must not disappear silently. |
| `attachments` | A file association remains tied to its message. Identical SHA-256 bytes may share blob storage but remain separate received-file records. |
| `integration_deliveries` | Unique destination and source promotion key; a retry returns the existing destination record or a reviewable conflict. |

### Scenario review

1. Shubham and Aarushi both know the same person. There is one `people` row, two `account_contacts` rows, and separate threads. A global suppression rule can stop a second invitation without hiding either account's history.
2. One person appears in a candidate campaign and a client campaign. The two memberships keep separate stages and source rows. The person's name, URLs, and organization affiliations remain shared.
3. A 500-row upload is repeated after a restart. `import_jobs` and `import_rows` record both files; existing person and membership keys prevent a silent duplicate send. Rejected rows are visible, and uncertain attempts remain reserved for review.
4. A reply arrives without a reliable profile link. The account-scoped thread and message remain visible; identity linking and campaign attribution become review items. A filename or text match does not merge people automatically.
5. A candidate sends a resume. The file, message, source account, permission evidence, and Cruitical delivery are separate. A missing email or permission stops promotion while the reply and file stay visible in LinkBound.

## Reuse assessment

| Candidate | Evidence | Decision for first releases |
| --- | --- | --- |
| [SQLAlchemy Core](https://docs.sqlalchemy.org/en/20/) and [Alembic](https://alembic.sqlalchemy.org/en/latest/) | Both support Python relational access and versioned migrations. SQLAlchemy documents [SQLite transaction behavior](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html). | Use for new tables and migrations, incrementally. Keep a compatibility adapter for current `sqlite3` call sites until reads and writes move safely. This saves migration and transaction plumbing without replacing the browser runner. |
| [SQLModel](https://sqlmodel.tiangolo.com/) | FastAPI-friendly Pydantic plus SQLAlchemy model layer. | Defer. LinkBound's API input models, storage schema, and provider observations need different validation rules; a shared model would save less than it first appears to. |
| [APScheduler](https://apscheduler.readthedocs.io/en/3.x/modules/jobstores/sqlalchemy.html) | Its SQLAlchemy job store persists scheduler jobs and serializes job state with pickle. | Optional clock only. The campaign target, lease, budget, and uncertain-send state remain in LinkBound tables. A single database-backed due-work poller is sufficient for the first deployment. |
| [RQ](https://python-rq.org/docs/), [Dramatiq](https://dramatiq.io/installation.html), [Temporal](https://docs.temporal.io/) | They add Redis, RabbitMQ, or a workflow service and still need LinkBound account locks and business state. | Defer until measured worker concurrency or workflow complexity justifies another service. |
| [Twenty](https://github.com/twentyhq/twenty/blob/main/LICENSE), [EspoCRM](https://github.com/espocrm/espocrm/blob/master/LICENSE.txt), [Chatwoot](https://github.com/chatwoot/chatwoot/blob/develop/LICENSE), [Mautic](https://github.com/mautic/mautic/blob/7.x/LICENSE.txt) | Full applications with their own runtime, storage, UI, and license terms. Twenty and EspoCRM are primarily AGPL; Chatwoot core is MIT with separately licensed enterprise code; Mautic is GPL. | Use their schemas and workflows as references. Installing one would create a second source of truth and still require custom LinkedIn browser and sync code. Revisit a full CRM integration if LinkBound becomes a multi-team CRM product. Review exact licenses before any code reuse. |
| Local file storage now, [fsspec](https://filesystem-spec.readthedocs.io/) later | fsspec can abstract local and object storage. | Keep bytes on a protected persistent volume with checksums and tested backups first. Add an adapter before moving to object storage. |

No reviewed project supplies a reusable LinkedIn browser inbox or invitation adapter. The tested LinkBound Playwright path remains the provider adapter, with CRM and scheduling state around it.

## Changes required in the architecture plan

1. Add organizations, affiliations, contact points, and source-qualified external identities to the foundation. Do not infer a real organization from `company_csv` alone.
2. Replace the single `campaign_targets` record that carried enrollment, lease, attempt, and outcome with membership, step, work item, and attempt records.
3. Add a channel identity and conversation participant link so a shared person can appear under several LinkedIn accounts without merging thread state.
4. Persist import rows, suppressions, human review items, ordinary follow-up tasks, and candidate permission evidence.
5. Define explicit uniqueness, attribution, and source coverage rules before implementing read models and UI statuses.
6. Use SQLAlchemy Core and Alembic incrementally, while keeping the one-process SQLite control plane and the existing browser path.

The benchmark does not add generic deal, opportunity, ticket, or fully configurable custom-object engines. LinkBound's immediate workflows are candidate and client outbound, response triage, file handling, and Cruitical promotion. A broader sales pipeline can be added when its stages and required actions are known.
