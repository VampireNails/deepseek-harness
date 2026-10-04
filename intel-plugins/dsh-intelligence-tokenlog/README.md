---
description: "Bounded token usage scans for Linux Host conversations."
kind: "package-bundle"
---

# Token usage

English | [中文](README.zh.md)

## Summary

This bundle adds a read-only token usage tool to Linux Host conversations. It attributes tokens to the provider and model recorded in retained logs, scans in a worker with time limits, and reports skipped files. It provides no billing estimate.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

<a id="use-this-package"></a>
## Use this package

To add this bundle to existing Linux `web` and `evolve` profiles and the sysevents bundle to `web`, run `node intel/hmc-observability.mjs <profiles-root> <new-backup-directory>` from the repository. The helper preserves the profile patches and backs up both manifests before changing them. Restart the production service to load the new bundles. To roll back, restore the backed-up manifests and remove only links marked `created` in `links.json`, then restart. HMC consumes system events from the shared log independently of which profile emitted them; this tool is available in conversations, not a dedicated Android usage screen.

`token_usage_summary(days)` reads the Linux Host's retained `session.v4.jsonl.zstd` logs for 1–30 days and reports input/output tokens by task, day and provider/model. Each assistant message uses its preceding request header, including changes within a session. Missing headers are reported as `unknown`; no provider is assumed. Session counts per model can overlap when a session switches models. The task bucket uses the first user message.

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals</summary>

The tool runs decompression in a worker so HTTPS and event delivery remain responsive. `timeoutMs` in the profile config bounds a scan (default 60000, allowed 1–300000). Each decompression is killed after the smaller of the remaining scan budget and `decompressTimeoutMs` (default 5000, allowed 1–300000). Cancellation waits for the current bounded decompression and worker exit; it is not immediate. Total deadline exhaustion fails the scan rather than returning partial success. Errors expose fixed codes. `ctx.get("tokenlog").aggregate(sessionsRoot, days, signal)` returns a Promise; `format` renders its result. The scan only reads logs and does not make model requests.

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

The report counts retained logs, not a billing ledger. Missing or unreadable session files are counted as skipped. Token records do not establish subscription entitlement, cache prices or monetary charges, so the report provides no cost estimate. Other session generations and separately stored child logs are outside this reader's scope.

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers</summary>

None.

</details>
