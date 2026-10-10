# dsh-intelligence-cron

English | [中文](README.zh.md)

cron_create parses a Chinese schedule and creates a reminder, cron_list lists reminder state, and cron_delete removes the wrapper record and underlying schedule. Create, list and delete report used capacity against the fixed effective limit of 20, including completed and foreign-session records. At or above the limit only additions reject; deletion, reconciliation and re-arming remain available. Config has dataDir and timeZone, without a limit override.

## Scheduling

Daily and weekly expressions become one-shot at schedules that re-arm after dispatch. Fixed intervals use every_seconds and require at least 300 seconds. Relative delays and tomorrow expressions create a single reminder. The supplied cordis.patch.yml loads @deepseek-ai/dsh-schedule as a plugin; it is not itself a profile bundle.

Reminders belong to the creating session and follow up only while that session is live. Reconciliation restores the next future occurrence without replaying missed reminders. Wrapper metadata lives in $DSH_HOME/intel-cron/cron.json. Tools and listeners use ctx.effect.

timeZone defaults to Asia/Shanghai; an empty value retains that default. Invalid nonempty IANA zones reject at scheduler construction. Daily, weekly and tomorrow expressions use that configured wall clock; intervals and relative delays use elapsed seconds. Valid wall times across New York DST transitions follow the changed offset. Nonexistent or repeated DST wall times retain the existing Intl conversion and one-slot-per-date behavior; the plugin does not promise two deliveries for a repeated hour.

## Persistence

cron.json is authoritative. The writer stores `{version:1, seq, jobs}` with a nondecreasing ID high water mark. Legacy arrays remain readable and convert on the next successful change, preserving every record and extension field; the current session's seen schedule IDs also prevent legacy deleted-ID reuse. Older array-only code cannot read the new envelope: retain its matching JSON backup before any code downgrade. Malformed JSON, invalid UTF8, invalid record fields, duplicate IDs and read errors reject; only an absent leaf in an accessible directory starts empty. No damaged records are discarded or repaired automatically.

Read/modify/write operations, including re-arm and reconciliation across agents, hold the existing `.writer.sqlite` exclusion convention. Same-process callers queue by canonical directory; asynchronous cross-process acquisition waits up to three seconds without blocking the event loop. JSON publication uses an exclusive owner-only temporary in the same directory, complete write, file fsync, rename and directory fsync. Before rename, write/sync/rename failure keeps the original complete JSON. Temporary cleanup touches only the writer's exclusive file. Direct CronStore.save is a low-level replacement and must run inside transaction when writers can overlap.

JSON and session schedules are separate commits. Create/delete publish JSON before appending and flushing schedule events; re-arm flushes the new slot before publishing its pointer. `CRON_*` errors expose safe errno and committed state without paths or reminder contents. Post-rename directory-sync or subsequent schedule-flush failure can have `committed:true`: read the record and reconcile, rather than replaying a non-idempotent create. Atomic replacement and exclusion do not promise power-loss exactly-once delivery or coordination with older writers that ignore the lock.

Dispatch re-arming failures emit `cron.rearm_failed` to [system events](../dsh-intelligence-sysevents/README.md). The event identifies the session, job and dispatched schedule slot with a stable derived run ID and safe code, excluding reminder names, prompts and raw error messages. Repeated reports of the same slot deduplicate. A failed flush leaves wrapper metadata on the old slot; a repeated dispatch retries the durability barrier for an already appended new slot before saving metadata. Event-write errors warn independently with the original code. Explicit test scope remains isolated; normal dispatch and successful recovery emit no exception notification.

## Removal and verification

Call cron_delete from the owning session before unloading to remove an outstanding schedule. Deleting a job from another session removes only wrapper metadata and leaves its persisted reminder active. Unloading alone removes re-arming listeners but an already persisted one-shot can still fire through DSH Schedule. Run node --test tests/parse.test.js tests/scheduler.test.js; tests/integration.sh separately checks an isolated profile and a real timer with a mock LLM.
