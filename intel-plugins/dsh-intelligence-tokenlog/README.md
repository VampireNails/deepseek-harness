---
description: "Bounded token usage scans for Linux Host conversations."
kind: "package-bundle"
---

# Token usage

English | [中文](README.zh.md)

## Summary

This bundle adds a read-only token usage tool to Linux Host conversations. It attributes tokens to the provider and model recorded in retained logs, scans in a worker with time limits, and fails when a retained source cannot be read completely. It provides no billing estimate.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

<a id="use-this-package"></a>
## Use this package

To add this bundle to existing Linux `web` and `evolve` profiles and the sysevents bundle to `web`, run `node intel/hmc-observability.mjs <profiles-root> <new-backup-directory>` from the repository. The helper preserves the profile patches and backs up both manifests before changing them. Restart the production service to load the new bundles. To roll back, restore the backed-up manifests and remove only links marked `created` in `links.json`, then restart. HMC consumes system events from the shared log independently of which profile emitted them. Its Android usage page reads this bundle's mounted aggregation service without creating a conversation or model request.

`token_usage_summary(days)` reads the Linux Host's retained `session.v4.jsonl.zstd` logs for 1–30 days and reports input/output tokens by task, day and provider/model. Each assistant message uses its preceding request header, including changes within a session. Missing headers are reported as `unknown`; no provider is assumed. Session counts per model can overlap when a session switches models. Task identities come from the read-only v1 automation index matched by the physical v4 `type: "session"` header ID, independent of the directory name, prompt, and automation run date. Native identities and audited `legacy-evidence:` identities have distinct labels; first-user keywords remain explicitly heuristic and unmatched logs remain unknown. The index contains no scope. The reader joins its immutable run/task ID to existing v1 `job_history.sqlite` runs and latest completions in read-only transactions; repeated completions count once. Only an exact, unique task/run match establishes production/test scope. Missing, pending, pruned or audited legacy runs remain unknown; mismatched tasks, competing scopes or cyclic subagent ancestry remain conflict. Task and scope are separate dimensions; task rows split by scope and `byScope` conserves every recorded token field and physical session count. No names or paths establish scope. The additive attribution version 2 fields declare source availability and complete/partial/unavailable scope coverage; old consumers must be upgraded before enabling this reader.

The window covers the preceding `days × 24` hours and can cross more calendar dates. Each retained session contributes only usage messages in that rolling window; dates use UTC, not the index run date. Task session counts partition unique header IDs, while per-model counts may overlap; the raw daily `sessions` field counts usage records and is not a unique-session count. Both the tool and aggregation service use the optional `sessionsRoot` profile setting; without it, they read `sessions/--root--` under `DSH_HOME`, or `~/.dsh` when that variable is unset. A missing or unreadable root fails with `TOKENLOG_SOURCE_UNAVAILABLE`; an existing empty root reports zero. A service caller can explicitly override the root for its scan. The optional `automationIndex` points to an existing attribution index; otherwise a canonical `sessions/--root--` root selects the sibling home’s `intel-joblog/automation-sessions.sqlite`. Absence keeps classification heuristic/unknown; an unreadable or corrupt index fails the scan. Identical duplicate sequence records and identical roots with the same header ID count once; conflicting duplicates fail.

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals</summary>

The tool runs decompression in a worker so HTTPS and event delivery remain responsive. `timeoutMs` in the profile config bounds a scan (default 60000, allowed 1–300000). Each decompression is killed after the smaller of the remaining scan budget and `decompressTimeoutMs` (default 5000, allowed 1–300000). Cancellation waits for the current bounded decompression and worker exit; it is not immediate. Total deadline exhaustion fails the scan rather than returning partial success. Errors expose fixed codes for missing/non-executable zstd, decompression, missing/unreadable logs, invalid JSON/UTF8, conflicting duplicates and metadata failures. The reader checks compressed-file access before invoking zstd; a permission change after that check can still appear as decompression failure. Tool failures throw so the registered executor returns `isError=true`. `ctx.get("tokenlog").aggregate(sessionsRoot, days, signal)` returns a Promise; `format` renders its result. `automationIndex` and `jobHistory` may select existing identity/completion files; otherwise they resolve beneath the configured DSH home. The scan fixes an exclusive end and inclusive start before reading, uses assistant event timestamps, and groups days in UTC. `aggregateSessions` and `aggregateInWorker` additionally accept a closed `window: {startMs,endMs}` for isolated validation. No file mtime, run date, completion date or current cumulative total selects window usage. Missing event timestamps fail instead of substituting a clock. The scan only reads logs and does not make model requests.

</details>

<a id="further-exploration"></a>
## Further Exploration

[Bundle patch](cordis.patch.yml) · [Plugin entry](index.js)

<a id="model-experience"></a>
## Model Experience

Indirectly, through the tool activated by this bundle; the formatted `token_usage_summary` result enters the conversation that invokes it.

#### KV Cache effect

Reading logs or checking credentials does not change model requests already in flight.

<a id="known-limitations-and-deferred-work"></a>
## Known Limitations and Deferred Work

The report counts retained logs, not a billing ledger. Any unsupported, missing, unreadable or corrupt retained file fails the scan; the reader does not return partial success. Input/output retain their disjoint uncached-input/output meaning. Cache-read/cache-write tokens and the number of records containing each field are separate; missing cache fields do not mean zero. Usage-record counts do not establish HTTP requests or attempts. Token records do not establish subscription entitlement, cache prices or monetary charges, so the report provides no cost estimate. Only the configured retained directory is scanned, including child logs stored there. Physical v4 IDs deduplicate identical copies; contradictory copies fail. Only verified subagent parent headers inherit an indexed automation identity; ordinary forks keep their own identity. A seeded header must have an inherited end-seed marker; its prefix is excluded, while unseeded end-seed markers retain live usage. Child sessions contribute their own usage once and count as separate physical sessions. Other session generations and directories are outside the source.

<a id="dev-note"></a>
Tool failures preserve the worker's exact supported TOKENLOG_* protocol codes in error.info.code and safe rendered content. Unknown worker exceptions become TOKENLOG_READ_FAILED. Cancellation and timeout remain failures; successful totals, task classification and unavailable billing semantics are unchanged.

## Dev Note

<details>
<summary>Working context for maintainers</summary>

None.

</details>
