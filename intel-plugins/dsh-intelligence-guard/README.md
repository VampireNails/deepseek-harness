# Production environment guard

The production launcher calls `checkInvariants` before starting the `web` profile. It rejects an overridden DeepSeek endpoint, mock environment variables and missing model credentials. The plugin itself reports activation errors; the launcher owns the fail-closed process exit.

A DeepSeek environment key or a structurally complete Codex OAuth grant satisfies the credential presence check. The grant is read from the versioned credentials document under `DSH_HOME` (otherwise the current user's `.dsh`), or the explicitly supplied check path. Comments, empty records and malformed YAML do not count. YAML errors and credential values are never returned. A stored grant does not establish current authorization, model entitlement or successful invocation; verify those through the Host model-access API.
