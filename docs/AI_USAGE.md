# AI usage and ownership

- The owner designed the Nimbus schema, safety invariants, approval state machine,
  ticket-linking policy, and KB coverage boundaries.
- AI assistance helped draft portions of the synthetic KB prose and implementation
  scaffolding. The owner reviewed the articles, removed unsupported security and
  feature claims, and checked the article IDs and coverage decisions against the
  linked ticket topics.
- No real customer information, credentials, or confidential data was used. The
  Tobi-Bueck source is public ticket text used as a language source and rewritten
  into the fictional Nimbus SaaS context.
- Phase 6 eval labels (`data/eval/eval_set.jsonl`): the first pass was drafted with AI
  assistance from the ticket text and `eval/LABELING_GUIDE.md`; the owner hand-reviews every
  row in batches and `reviewed`/`reviewed_by` are set only for accepted rows. Rows still
  unreviewed are reported as such by `eval/report.py` and must not be presented as the
  headline. Four rows (a plan upgrade, a refund-policy question, an in-window refund, and an
  injection/clean-control pair) were authored specifically to cover branches the source
  dataset does not contain; they are marked `source: "authored"`.
- Phase 6 failure analysis (`docs/FAILURE_ANALYSIS.md`) was written from real run records,
  not generated prose; every excerpt is copied from `trace_events` or the run JSON.
