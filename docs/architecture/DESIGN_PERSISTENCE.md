# M15 Design Persistence Invariants

SQLite is the authoritative M15 store for project design messages, structured
decisions, and complete project-document revisions. Provider sessions and files are
not authoritative design history.

The persistence boundary applies these invariants:

- message retrieval is project-scoped and ordered by occurrence timestamp and then
  stable internal message ID;
- external deliveries are unique by platform, chat, and external message ID, while
  the Telegram polling cursor remains an independent recovery mechanism;
- document revisions increase independently for each project and document type, and a
  draft has no supersession lineage until it is approved;
- document content is stored in full and its SHA-256 digest is calculated over the
  exact content encoded as UTF-8;
- document revision content, hash, identity, type, and revision number are immutable;
- superseding an approved document and approving its replacement are one database
  transaction; the replacement records the prior approved revision it actually
  supersedes, and a partial unique index allows at most one active approval;
- structured decisions are retained after supersession, while current design context
  includes only non-superseded decisions; and
- context reconstruction returns typed project, message, decision, and document
  records rather than an Architect prompt or provider payload.

M16 and later code must use the structured context service rather than querying these
tables directly or relying on AI conversation memory.
