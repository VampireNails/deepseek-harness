# Agent Note: Explicit Session retirement

Status: implemented

English | [中文](2026-10-13-explicit-session-retirement.zh.md)

## Problem

Disposing a live Agent releases ownership but retains its durable Session. Archiving changes discovery metadata. Neither operation supplies an irreversible deletion contract, and removing the current log alone lets retained generations revive the identity.

## Decision

An explicitly authorized Linux persistence deletion retires an exact ordinary cold Session. The JSONL provider holds its existing process-local writer claim and cross-process flock, validates all generation identities and directory ownership, durably publishes an exclusive root-wide retirement record, then removes only its recorded log generations. The stable lock inode and retirement record remain. Retirement is independent of adjacent format migration: migration continues to preserve every committed predecessor; an explicit deletion intentionally removes the retired Session's generations.

Admission refuses every live Session or attached descendant, including an idle Agent that retains a writer, and active durable Schedule records. The provider never obtains disposal rights belonging to another owner. A missing durable-object checker refuses instead of assuming inactivity. The business controller removes only the retired identity's workspace associations and emits the existing removal notification after physical cleanup; retry repeats that bookkeeping. Shared memory, artifacts, caches and independent Sessions remain outside deletion ownership.

## Alternatives considered

**Disposal or archival:** both intentionally preserve durable history, so neither prevents restart recovery.

**Remove the directory:** unlinking the flock inode permits a new inode to admit another writer; broad directory removal also makes unknown files part of deletion ownership.

**Delete the current generation alone:** historical generations become discoverable again. A durable identity record is required before removing any generation.

**Force stop all activities:** the caller does not own arbitrary Agent handles. Refusing a live owner preserves the existing ownership model; the initiator closes its own handle before retrying.

## Consequences

The supported discovery scope is `cold-ordinary-session`. Turn completion or cancellation alone is insufficient. Live-owner deletion and subagent-object deletion remain unsupported; SDK-created live Sessions become deletable after their runtime releases ownership. Permanent identity retirement trades same-ID reuse for restart safety. Successful cleanup leaves a lock file and retirement record. An incomplete cleanup remains hidden, and absence from a list cannot acknowledge physical removal. The [provider contract](../../../../packages/session/session-persistence-jsonl/README.md#irreversible-deletion-on-linux) owns outcomes, cancellation and filesystem assumptions.

## Verification

The provider's Linux deletion tests exercise exact-ID neighbors, link and header rejection, compression, restart, cancellation and partial cleanup. SDK tests exercise actual persistence ownership and response decoding. A supported named Linux Host profile exercises the real Typert and HMC HTTPS methods, running turns, jobs, durable reminders, ownership release, and reconnect. A separate Linux process probe exercises flock contention and SIGKILL after marker publication before unlink. SIGKILL recovery is evidence for process failure; it is not a physical power-loss experiment.
