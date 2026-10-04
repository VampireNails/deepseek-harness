---
description: "Startup environment and credential presence checks for the production profile."
kind: "package-bundle"
---

# Production environment guard

English | [中文](README.zh.md)

## Summary

The production launcher calls `checkInvariants` before starting the `web` profile. It rejects an overridden DeepSeek endpoint, mock environment variables and missing model credentials. The plugin itself reports activation errors; the launcher owns the fail-closed process exit.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

<a id="use-this-package"></a>
## Use this package

The production launcher calls `checkInvariants` before starting the `web` profile. It rejects an overridden DeepSeek endpoint, mock environment variables and missing model credentials. The plugin itself reports activation errors; the launcher owns the fail-closed process exit.

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals</summary>

A DeepSeek environment key or a structurally complete Codex OAuth grant satisfies the credential presence check. The grant is read from the versioned credentials document under `DSH_HOME` (otherwise the current user's `.dsh`), or the explicitly supplied check path. Comments, empty records and malformed YAML do not count. YAML errors and credential values are never returned. A stored grant does not establish current authorization, model entitlement or successful invocation; verify those through the Host model-access API.

</details>

<a id="further-exploration"></a>
## Further Exploration

[Bundle patch](cordis.patch.yml) · [Plugin entry](index.js)

<a id="model-experience"></a>
## Model Experience

Indirectly, through the startup check deciding whether the profile starts; the guard makes no model request.

#### KV Cache effect

Reading logs or checking credentials does not change model requests already in flight.

<a id="known-limitations-and-deferred-work"></a>
## Known Limitations and Deferred Work

Credential presence does not establish model entitlement or successful invocation; use the Host model-access API for a connection check.

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers</summary>

None.

</details>
