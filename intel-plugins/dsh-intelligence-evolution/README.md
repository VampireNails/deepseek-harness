---
description: "Configure original user-input capture and bounded memory upkeep through the existing DSH Host."
kind: "package-bundle"
---

# Evolution and upkeep source

English | [中文](README.zh.md)

## Summary

Capture new original user-channel input with authorship labels for memory upkeep through the existing Host. Empty batches can skip model execution, and uncertain writes remain pending for review. Task templates, heartbeat checklist access and native automation-session attribution support the [Linux runner](../../intel/task-runner/README.md), which owns scheduling, model routes, checkpoints and pending-run recovery.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

<a id="use-this-package"></a>
## Use this package

Configure `upkeepSourceSocket` only in the existing Host profile that supplies sessions, sessionQuery and sessionPersistence. Its default is empty, so runner profiles expose no source server. The absolute Linux socket path must match the runner's `upkeep.socketPath`. The plugin requires a private owner-only parent directory, publishes a mode-600 Unix socket, refuses active sockets and regular files, and removes only its own published inode during teardown. Requests support raw capture and exact-run inspection; they cannot launch agents, execute tools or mutate sessions. Reads abort on disconnect or the configured deadline, and teardown awaits owned reads.

`upkeepSourceTimeoutMs` defaults to 30,000 milliseconds. `upkeepMaxSessionBytes` defaults to 67,108,864 compressed bytes per cold session artifact.

`upkeepMachineSessionIds` defaults to an empty list and excludes whole roots reserved for controlled machine acceptance input. `upkeepMessageOrigins` defaults to an empty list of exact `{sessionId, seq, messageId, kind}` assignments, where `kind` is `human` or `machine`. The Host owner must verify authorship independently before assigning a message; a `user` channel, working directory or message text is insufficient. Human assignments cannot conflict with machine session scopes, duplicate or invalid assignments reject source startup, and assignments never transfer to forked copies. Configure controlled acceptance scopes before capture. This optional private configuration does not mutate historical source events or establish human authorship for unassigned messages.

`heartbeat_check` reads the private `intel-evolution/HEARTBEAT.md` checklist in groups of three. The shipped cron check uses `joblog_status` and `joblog_alerts`; `route-runs.jsonl` supplies run/exit details when needed. Unfinished starts remain unconfirmed, quiet upkeep skips are expected, and historical alerts retain their dates. Updating this package does not overwrite a custom private checklist; back it up and compare the shipped cron entry before migrating it.

The source waits for all three reader services, including providers registered later during Host startup. It publishes the socket only after they become available; eligible source setup failures reject startup. The source belongs to the evolution plugin's lifecycle and closes when that plugin is disposed.

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Read ownership and cursor handling</summary>

Cold reads are sequential and use official read-only persistence handles. Live reads use immutable event snapshots. Neither path attaches cold agents, follows updates, or adds synthetic interruption closers to the raw cursor. Missing, unreadable or oversized source logs fail the batch rather than looking empty.

Capture includes original `user/message` events whose source kind is `user`, excluding inherited fork events, canonical subagents, indexed automation and explicitly attributed machine input. Messages carry `inputOrigin`: `human` requires an exact Host assignment; all unassigned messages remain `unknown` and available for bounded analysis. Bootstrap checkpoints current events without replaying them. Ordinary forks retain their own later input as unknown unless separately assigned. The source rechecks automation attribution after capture. The configured batch limits preserve complete originals and retain cursors for input deferred to later batches.

</details>

<a id="further-exploration"></a>
## Further Exploration

- [Runner admission and pending recovery](../../intel/task-runner/README.md)
- [Memory persistence and write metadata](../dsh-intelligence-memory/README.md)
- [Cordis effects and disposal](../../docs/cordis-primer.md)

<a id="model-experience"></a>
## Model Experience

Dreaming starts with `joblog_status` and `joblog_alerts`, then pairs dated `route-runs.jsonl` records by run ID. Latest success does not erase earlier failures; usage limits and invalid authorization tokens require separate original error evidence. The prompt budgets ten calls including errors, at most two lesson writes, and one reserved Feed publication. Its final runner input preserves the prohibition on `artifact_save`. It stops investigation when evidence is unavailable or the budget is reached and publishes the checked scope and gaps. This is prompt guidance, not a runtime tool cap; natural runs still require call-count and publication review.

For runner identities `evolve_upkeep` and `memory_upkeep`, actual tool admission permits only top-level native `memory_search` and `memory_write`. `upkeepToolBudget` defaults to six calls per agent, including failed calls. Other task identities retain their existing tools. The upkeep prompt treats the supplied original batch as source data; unknown authorship remains uncertain and must not be reported as verified human input or used to infer preferences from acceptance instructions. Explicit facts can still be analyzed without discarding all unknown originals. Memory search only checks duplicates or conflicts. Persisted completion inspection requires the exact attributed root/run, a completed turn, settled matching tool calls and canonical successful memory-write metadata. Denials, tool errors and failed writes keep the run unconfirmed.

<a id="known-limitations-and-deferred-work"></a>
## Known Limitations and Deferred Work

The compressed byte check bounds the input artifact, not decompressed memory usage; the provider currently decodes a whole session log. Quiet-work tests and shortened backoff clocks do not establish a production 24-hour sample.

<a id="dev-note"></a>
## Dev Note

Run `node --test intel-plugins/dsh-intelligence-evolution/tests/*.test.js` on Linux after installing the plugin's locked dependencies. The raw-source fixtures use the released Session, JSONL persistence and SQLite query providers without starting another Host or requesting a model.
