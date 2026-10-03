---
description: "Host Remote owner for settings and credential configuration surfaces, including redacted reads, writes, credential references, and native document opening."
kind: "package-reference"
---
# Settings Controller

English | [中文](README.zh.md)

## Summary

`@deepseek-ai/dsh-api-settings-controller` exposes generated `ctx.remote.settings`, `ctx.remote.credentials`, and `ctx.remote.modelAccess` namespaces for configuration pages. It returns redacted settings and credential metadata, supports writes without returning secret values, manages subscription sign-in, and opens provider-owned settings locations on the Host desktop. Missing providers produce an explicit invocation error.

## Table of Contents

- [Use this package](#use-this-package)
- [Configuration](#configuration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

-----

<a id="use-this-package"></a>
## Use this package

Mount this package as a Loader entry in a profile that serves browser configuration. The entry registers all four namespaces independently of their providers so a missing provider produces a named configuration error at invocation. Its generated descriptors enter the strict Typert registry, while the settings and credential Definitions remain plain Cordis Services with no wire obligations of their own.

`describe(refs)` answers one map keyed by the requested names, so a settings page describing every reference its rows carry settles those rows together. It accepts at most 64 names per call, reports an invalid name or empty write value as `bad-request`, and copies each answer field by field — a provider returning more than `CredentialInfo` declares cannot widen what crosses. Valid `set(ref, value)` and `unset(ref)` calls report a provider refusal as `credential-rejected`, carrying the provider's message with only the reference in its details. Secret values cross in this direction only: no method here returns one.

`settings.describe()` returns deployment facts and every namespace under `redactSecrets: true`. `settings.update`, `settings.replace`, and `settings.mutate` expose the settings service's three write operations and return the namespace's new redacted view; stale writes use `settings-conflict` and other provider refusals use `settings-rejected`.

`settings.openSettingsDocument()` prepares the provider-owned document and opens it with the native text editor; it accepts no browser-supplied filesystem target.

`modelAccess.configuration()` includes installed dormant providers, enabled profiles, namespace revisions, offered login methods, and separate reference/record metadata. Editable providers declare `credentialScope: 'llm-pi-ai'` in the LLM directory. `save(request)` requires an explicit authentication mode and current revision; newly entered API keys use dedicated Host references. OAuth activation requires a stored grant and rejects inherited endpoint, protocol, key-reference, or header overrides. `remove` removes only user configuration and refuses an inherited provider; `clearKey` removes the page-managed reference and API-key record; `logout` deletes only a subscription grant. The selected authentication mode, stored credential presence, and verified invocation are separate facts.

`start({ provider, method })` returns a random attempt ID and owner token immediately. `status`, `respond`, and `cancel` require both. Polling returns the current notice and prompt without retaining answers. A flow withdrawing its prompt invalidates that prompt ID. Same-provider concurrent attempts are refused; attempts expire and terminal results have bounded retention. Errors use safe reason codes without forwarding credential or provider error text.

`verify({ provider, model })` performs a fixed request capped at 16 output tokens and returns only success or a safe failure code. `discover(request)` delegates model discovery to the installed Host adapter and returns IDs and names. Read-only providers return their registered model catalog and reject draft endpoint, protocol, and key overrides; their configuration remains read-only.

-----

<a id="configuration"></a>
## Configuration

| Field | Default | Meaning |
|---|---|---|
| `modelAccess.attemptTtlMs` | `600000` | Authorization lifetime, 1000–3600000 ms |
| `modelAccess.retentionMs` | `300000` | Terminal result retention, 1000–3600000 ms |
| `modelAccess.maxAttempts` | `32` | Maximum running and retained attempts, 1–256 |
| `modelAccess.verificationTimeoutMs` | `30000` | Connection check deadline, 1000–120000 ms |

The generated [configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-api-settings-controller) is the exhaustive source for accepted fields and their JSDoc.

-----

<a id="model-experience"></a>
## Model Experience

Configuration and authorization register no prompt, tool, or session event. An explicit connection check sends the fixed user input `Reply OK.` to the selected model and caps output at 16 tokens; it consumes provider usage and returns no response text.

#### KV Cache effect

No direct effect; reading or writing these configuration values does not alter model requests already in flight.

## Known Limitations and Deferred Work

<a id="known-limitations-and-deferred-work"></a>

- The batch bound is fixed at 64 references and is not a deployment-configurable field.
- Profile and credential storage are separate commits. A credential refusal after a profile commit returns `configuration-saved-credential-failed`; re-read configuration before retrying. Stale or invalid profile writes never change a key.
- Model account editing currently supports pi-ai providers. Other adapters remain readable in the directory.
- Credential-source metadata covers stored records and explicit `apiKeyEnv` references. Provider-owned ambient discovery may still authenticate after logout; it is not enumerated or cleared by this API.

<a id="dev-note"></a>
### Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

Built wire acceptance uses `tests/model-access-wire.e2e.ts` and requires this package’s generated `lib/typert.host.js`. Ordinary source tests run without that artifact.

</details>

**Runtime invariant:** No companion is published. The settings and credential seams own storage and update events, while this package only projects their methods onto the wire.
