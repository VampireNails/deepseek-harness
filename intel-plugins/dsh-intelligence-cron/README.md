# dsh-intelligence-cron

English | [中文](README.zh.md)

cron_create parses a Chinese schedule and creates a reminder, cron_list lists reminder state, and cron_delete removes the wrapper record and underlying schedule. The plugin limits jobs to 20 and uses c1, c2 and subsequent IDs.

## Scheduling

Daily and weekly expressions become one-shot at schedules that re-arm after dispatch. Fixed intervals use every_seconds and require at least 300 seconds. Relative delays and tomorrow expressions create a single reminder. The supplied cordis.patch.yml loads @deepseek-ai/dsh-schedule as a plugin; it is not itself a profile bundle.

Reminders belong to the creating session and follow up only while that session is live. Reconciliation restores the next future occurrence without replaying missed reminders. Wrapper metadata lives in $DSH_HOME/intel-cron/cron.json. Tools and listeners use ctx.effect.

## Removal and verification

Call cron_delete from the owning session before unloading to remove an outstanding schedule. Deleting a job from another session removes only wrapper metadata and leaves its persisted reminder active. Unloading alone removes re-arming listeners but an already persisted one-shot can still fire through DSH Schedule. Run node --test tests/parse.test.js tests/scheduler.test.js; tests/integration.sh separately checks an isolated profile and a real timer with a mock LLM.
