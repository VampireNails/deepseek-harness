# System events

English | [中文](README.zh.md)

System events persist exceptions for HMC's authenticated read-only `muse/system-events/list`. The plugin exposes emission and debugging tools plus a programmatic `sysevents` service. The runner and cron scheduler also use the same store directly, so failures do not require a working model or this plugin to be mounted.

## Storage and identity

The directory is `$DSH_HOME/intel-system-events`, defaulting to `~/.dsh/intel-system-events`. Production uses `events.jsonl`; explicit `INTEL_JOBLOG_SCOPE=test` or entry `scope: test` uses `events.test.jsonl`. Other scopes reject. Existing events without a scope remain readable; no name-based reclassification or migration occurs. Default listing excludes explicit tests; programmatic `listEvents({includeTests:true})` adds them.

Task events include stable `type`, `source`, `taskId`, `runId`, safe `code`, exit category, and applicable exit/session/schedule fields. Runner run IDs correlate with JobLog and route audit; cron derives its ID from the owning session and dispatched slot. Title and detail preserve the existing client fields. Producers exclude prompts, names, credentials, paths and raw error bodies; arbitrary extra fields are not persisted.

Structured identity determines the event ID within a scope. Under the shared SQLite writer lock, an identical report returns the first event and timestamp; conflicting reports reject `SYSEVENTS_IDENTITY_CONFLICT`. Unstructured tool emissions retain unique IDs. Deduplication applies while the source file is retained; rotation loses older identities.

## Failures and recovery

Writes append and fsync before returning. Missing sources list as empty; malformed, unreadable and oversized sources report safe `SYSEVENTS_*` errors. The 16 MiB source bound rejects further access rather than silently truncating deduplication. HMC scans a bounded tail; complete malformed rows fail explicitly, while an incomplete append waits for the next poll. An empty or absent source does not imply a broken channel, and a bounded read does not prove delivery across rotation.

A failed append or fsync may already have written bytes. Keep the original task outcome and inspect the event ID before retrying notification; never replay the task to repair telemetry. Stop writers and preserve the directory, including `.writer.sqlite`, before repairing or rotating a damaged source. A partial last line requires operator repair; writers do not erase it or fabricate successful delivery. Old writers do not participate in the shared lock and must be stopped before switching versions.
