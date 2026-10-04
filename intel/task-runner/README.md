# Linux task runner

English | [中文](README.zh.md)

This runner resolves built-in cron task IDs against the live evolution `TASKS` templates. An optional old prompt is ignored for built-in IDs; custom jobs require their explicit prompt. `routes.json` supplies task routes, the default provider/model and timeout. The runner launches only the `evolve` DSH profile, with an explicit model patch. It requires installed profile dependencies and normal provider credentials; it does not install providers or image tools.

`route-runs.jsonl` records requested provider/model, template ID, process exits and fallback reasons without prompts or credentials. Requested identity does not prove the effective model: correlate successful requests with tokenlog. Retrying a nonzero exit may duplicate side effects, so default routes disable retries. Enable `retrySafe` only for an audited read-only task. Timeout terminates the process group; an exit failure can still mean partial effects and requires review.

Before deployment, back up `crontab -l`, then pipe it to `node intel/task-runner/cli.mjs --migrate-crontab` and compare the preview. This command prints a replacement but never writes crontab. It migrates only registered built-in jobs with a single quoted literal prompt; custom jobs, compound commands, redirects and environment-specific wrappers stay unchanged for manual review. After review, deploy `run.sh` with executable permission, preserve cron schedules/timezones and inspect the first run. Do not blindly rerun a failed task. Existing profile configuration and production cron are not changed by adding this directory.

Run `node --test intel/task-runner/tests/*.test.mjs` on Linux. Isolated CLI tests use a fake `dsh` executable and never invoke production models.

The production route assignments are preserved: upkeep and heartbeat use Flash; morning briefing and the other six evolution jobs use ChatGPT. `bash intel/verify-linux.sh` installs each plugin's locked dependencies and runs the Linux plugin/runner regressions; run it in a disposable checkout because it replaces plugin dependency directories.
