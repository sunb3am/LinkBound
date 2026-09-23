# CRM and workflow benchmark for LinkBound v3

Reviewed 2026-09-22. Closed-source CRM documentation exposes logical objects and APIs, not internal database schemas. Open-source repository models provide implementation evidence. The [architecture plan](linkbound-v3-architecture-plan.md) contains the current, narrower implementation decisions. The primitive map below is a research catalog, not a table checklist.

## What existing systems actually separate

| System | Implemented distinction | Consequence for LinkBound |
| --- | --- | --- |
| [Attio](https://attio.com/help/reference/attio-101/attios-data-model/understanding-lists) | A person is a reusable record. A list entry holds process-specific fields, and the same person can have separate entries for different recruiting applications. | Keep person identity separate from campaign enrollment and candidate or client workflow state. |
| [Salesforce](https://developer.salesforce.com/docs/data/data-cloud-dmo-mapping/guide/c360dm-si-campaignmemberdmo-dmo.html) | Campaign Member links a campaign to a lead or contact and holds its own status and response date. | A campaign member is a durable entity, not a field on the contact or a send job. |
| [HubSpot](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/schemas/guide) | Records have properties and explicit associations. Activities such as notes, tasks, emails, and conversations can associate with records. [Contact-company associations](https://knowledge.hubspot.com/records/associate-records) can designate a primary company. | Keep company text and its source now. Add organization records when LinkBound needs company-level workflows. |
| [EspoCRM](https://docs.espocrm.com/user-guide/campaigns/) | Campaigns use inclusion and exclusion target lists and a campaign event log. [Relationships](https://docs.espocrm.com/development/api/relationships/) are first-class API operations. | Check prior confirmed sends and an explicit do-not-contact flag before outreach. Richer suppression rules can wait. |
| [Chatwoot](https://github.com/chatwoot/chatwoot/blob/develop/app/models/contact_inbox.rb) | A shared contact maps to a channel inbox through `ContactInbox`, unique on `(inbox_id, source_id)`. It owns conversations, which own [messages](https://github.com/chatwoot/chatwoot/blob/develop/app/models/message.rb) and [attachments](https://github.com/chatwoot/chatwoot/blob/develop/app/models/attachment.rb). | Preserve one person across sender accounts while storing account-specific provider identity, threads, messages, and files. |
| Mautic | [Campaign steps](https://github.com/mautic/mautic/blob/7.x/app/bundles/CampaignBundle/Entity/Event.php) and [per-contact execution logs](https://github.com/mautic/mautic/blob/7.x/app/bundles/CampaignBundle/Entity/LeadEventLog.php) are separate. Logs hold schedule, channel, result, and failure metadata, with a unique event, lead, and rotation key. | Preserve immutable attempt history now. Introduce separate steps if outbound sequences become a requirement. |
| [Twenty](https://github.com/twentyhq/twenty) | People, companies, tasks, notes, and custom objects form a configurable CRM application. | Useful model reference. Its full application is a separate deployment and integration choice. |

These products do not document a ready-made model for multiple LinkedIn sender profiles, browser profile locking, invitation uncertainty, or LinkedIn inbox observations. Those are LinkBound-specific records and policies. A CRM owner or assignee is a human responsibility field; a LinkedIn sender account is a channel identity. This distinction is an inference from the documented models, not a quoted vendor rule.

## Industry primitive map

| Layer | Record and ownership | Invariant |
| --- | --- | --- |
| Identity | `people`, `person_identities`, `contact_points`, `organizations`, `person_affiliations` | A person can have multiple observed URLs, email addresses, and company affiliations. Unknown or conflicting identity stays unresolved; no merge by name alone. |
| Sender relationship | `accounts`, `account_contacts` | One relationship per sender account and person. Connection evidence, contact state, and last sync are account-scoped. |
| Business context | `campaigns`, `campaign_memberships`, `campaign_steps` | One person can participate in several candidate or client campaigns. Enrollment status and context are logically independent of identity; LinkBound can represent one-step enrollment in `campaign_targets` for now. |
| Execution | `work_items`, `send_attempts`, `activity_events` | A durable due item can be leased. Every attempted browser action has an immutable result; an uncertain outcome cannot silently become a retry. |
| Inbox | `conversations`, `conversation_participants`, `messages`, `attachments` | Threads and messages belong to the sender account. Provider IDs are unique within their provider and account scope; weak fingerprints carry match confidence. |
| Human decisions | `review_items`, `tasks`, `suppression_rules`, `consent_records` | Exceptions and ordinary follow-ups are distinct. Both have a durable owner, source, status, and resolution; tasks also have a due time. |
| Intake and integration | `import_jobs`, `import_rows`, `sync_runs`, `integration_deliveries` | Original row, mapping, rejection, sync coverage, and external delivery remain traceable and idempotent. |

An `activity_events` layer is useful if several source tables eventually make timeline queries difficult. The first version can read existing outbound requests and new messages directly. A reply is attached to an account contact and conversation first. Campaign attribution is made only when an outbound touch can be linked to the reply, or after review. This prevents a reply from being assigned to whichever campaign was most recently imported.

### Cardinality and idempotency checks

| Record | Constraint or rule |
| --- | --- |
| `account_contacts` | Unique `(operator, normalized_linkedin_url)`; a provider source ID is unique only within its sender account and provider scope if one is exposed. |
| `campaign_targets` | Unique `(campaign_id, normalized_linkedin_url)` for the one-action campaign. Claims use a transaction, lease token, and conditional update. |
| `outbound_requests` | Keep append-only attempt rows linked to targets; a confirmed send and an uncertain result survive later skips or failures. |
| `messages` | Unique `(account_id, provider_message_id)` when a stable ID exists. A fallback fingerprint is a candidate match with confidence; a repeated identical message must not disappear silently. |
| `attachments` | A file association remains tied to its message. Identical SHA-256 bytes may share blob storage but remain separate received-file records. |
| `integration_deliveries` | Unique destination and source promotion key; a retry returns the existing destination record or a reviewable conflict. |

### Scenario review

1. Shubham and Aarushi both know the same person. One `contacts` row holds shared profile details; two `account_contacts` rows and separate threads preserve sender-specific history. A confirmed prior send can suppress a second invitation without hiding either account's history.
2. One person appears in a candidate campaign and a client campaign. The two `campaign_targets` rows keep separate queue state while sharing the contact's profile details.
3. A 500-row upload is repeated after a restart. The saved CSV, target uniqueness, and immutable request history prevent a silent duplicate send. Rejected row numbers remain in a validation report; uncertain attempts stay reserved for review.
4. A reply arrives without a reliable profile link. The account-scoped thread and message remain visible with a nullable contact link. A filename or text match does not merge contacts automatically.
5. A candidate sends a resume. The file, message, source account, permission evidence, and Cruitical delivery stay traceable. A missing email or permission stops promotion while the reply and file remain visible in LinkBound.

## Reuse assessment

| Candidate | Evidence | Decision for first releases |
| --- | --- | --- |
| [SQLAlchemy Core](https://docs.sqlalchemy.org/en/20/) and [Alembic](https://alembic.sqlalchemy.org/en/latest/) | Both support Python relational access and versioned migrations. SQLAlchemy documents [SQLite transaction behavior](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html). | Use Alembic for migrations. Keep the current runtime `sqlite3` access; adding SQLAlchemy Core alongside it now would create two persistence styles without a demonstrated benefit. |
| [SQLModel](https://sqlmodel.tiangolo.com/) | FastAPI-friendly Pydantic plus SQLAlchemy model layer. | Defer. LinkBound's API input models, storage schema, and provider observations need different validation rules; a shared model would save less than it first appears to. |
| [APScheduler](https://apscheduler.readthedocs.io/en/3.x/modules/jobstores/sqlalchemy.html) | Its SQLAlchemy job store persists scheduler jobs and serializes job state with pickle. | Optional clock only. The campaign target, lease, budget, and uncertain-send state remain in LinkBound tables. A single database-backed due-work poller is sufficient for the first deployment. |
| [RQ](https://python-rq.org/docs/), [Dramatiq](https://dramatiq.io/installation.html), [Temporal](https://docs.temporal.io/) | They add Redis, RabbitMQ, or a workflow service and still need LinkBound account locks and business state. | Defer until measured worker concurrency or workflow complexity justifies another service. |
| [Twenty](https://github.com/twentyhq/twenty/blob/main/LICENSE), [EspoCRM](https://github.com/espocrm/espocrm/blob/master/LICENSE.txt), [Chatwoot](https://github.com/chatwoot/chatwoot/blob/develop/LICENSE), [Mautic](https://github.com/mautic/mautic/blob/7.x/LICENSE.txt) | Full applications with their own runtime, storage, UI, and license terms. Twenty and EspoCRM are primarily AGPL; Chatwoot core is MIT with separately licensed enterprise code; Mautic is GPL. | Use their schemas and workflows as references. Installing one would create a second source of truth and still require custom LinkedIn browser and sync code. Revisit a full CRM integration if LinkBound becomes a multi-team CRM product. Review exact licenses before any code reuse. |
| Local file storage now, [fsspec](https://filesystem-spec.readthedocs.io/) later | fsspec can abstract local and object storage. | Keep bytes on a protected persistent volume with checksums and tested backups first. Add an adapter before moving to object storage. |

No reviewed project supplies a reusable LinkedIn browser inbox or invitation adapter. The tested LinkBound Playwright path remains the provider adapter, with CRM and scheduling state around it.

## Scope review: which patterns to use now

1. Keep the logical distinction between a shared contact and each LinkedIn sender's relationship to that contact. Reuse `operators` and `contacts`; add only an account-contact relation now. More general provider identities, organizations, affiliations, and contact points wait for evidence that the current URL and CSV fields cannot support a requested workflow.
2. Keep a distinct durable campaign target and immutable attempt history. Since the first queue supports one action per target, the target can also carry lease and queue state. Separate membership, steps, and work items become necessary only when multi-step outbound follow-ups are actually specified.
3. Add account-scoped conversations, messages, and attachments for the requested daily sync. A participant junction table and general inbox engine wait until group conversations or other channels matter.
4. Derive the initial attention view from uncertain requests, unmatched conversations, and sync errors. Do-not-contact can be a simple explicit flag. Generic review-item, task, suppression-rule, and event frameworks wait for real requirements for assignment, due dates, or policy complexity.
5. Keep runtime `sqlite3`. Use Alembic for versioned schema changes; SQLAlchemy Core, SQLModel, and a new persistence layer are deferred until current SQL becomes a limiting factor. The current browser path remains the provider adapter.

This correction leaves generic deal, opportunity, ticket, and custom-object engines outside LinkBound. It also moves the Linode headed-browser feasibility test ahead of major queue work, because the requested end state is an always-on automation suite.
