---
description: "Configure original human-input capture and bounded memory upkeep through the existing DSH Host."
kind: "package-bundle"
---

# Evolution and upkeep source

English | [中文](README.zh.md)

## Summary

Capture new original human input for memory upkeep through the existing Host. Empty batches can skip model execution, and uncertain writes remain pending for review. Task templates, heartbeat checklist access and native automation-session attribution support the [Linux runner](../../intel/task-runner/README.md), which owns scheduling, model routes, checkpoints and pending-run recovery.

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

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Read ownership and cursor handling</summary>

Cold reads are sequential and use official read-only persistence handles. Live reads use immutable event snapshots. Neither path attaches cold agents, follows updates, or adds synthetic interruption closers to the raw cursor. Missing, unreadable or oversized source logs fail the batch rather than looking empty.

Capture includes original `user/message` events whose source kind is `user`, excluding inherited fork events, canonical subagents and roots recorded in the automation index. Unknown historical roots remain unclassified; bootstrap checkpoints their current events without replaying them. Ordinary forks retain their own later human input. The source rechecks automation attribution after capture. The configured batch limits preserve complete originals and retain cursors for input deferred to later batches.

</details>

<a id="further-exploration"></a>
## Further Exploration

- [Runner admission and pending recovery](../../intel/task-runner/README.md)
- [Memory persistence and write metadata](../dsh-intelligence-memory/README.md)
- [Cordis effects and disposal](../../docs/cordis-primer.md)

<a id="model-experience"></a>
## Model Experience

For runner identities `evolve_upkeep` and `memory_upkeep`, actual tool admission permits only top-level native `memory_search` and `memory_write`. `upkeepToolBudget` defaults to six calls per agent, including failed calls. Other task identities retain their existing tools. The upkeep prompt treats the supplied original batch as source data; memory search only checks duplicates or conflicts. Persisted completion inspection requires the exact attributed root/run, a completed turn, settled matching tool calls and canonical successful memory-write metadata. Denials, tool errors and failed writes keep the run unconfirmed.

<a id="known-limitations-and-deferred-work"></a>
## Known Limitations and Deferred Work

The compressed byte check bounds the input artifact, not decompressed memory usage; the provider currently decodes a whole session log. Quiet-work tests and shortened backoff clocks do not establish a production 24-hour sample.

<a id="dev-note"></a>
## Dev Note

Run `node --test intel-plugins/dsh-intelligence-evolution/tests/*.test.js` on Linux after installing the plugin's locked dependencies. The raw-source fixtures use the released Session, JSONL persistence and SQLite query providers without starting another Host or requesting a model.
