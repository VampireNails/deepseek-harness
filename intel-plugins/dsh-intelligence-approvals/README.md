# dsh-intelligence-approvals

English | [中文](README.zh.md)

This out-of-tree plugin classifies tools before execution and reuses DSH's approval service. READ allows execution, WRITE requests approval, EXEC first checks dangerous command patterns then requests approval, and BLOCKED denies execution without an approval card. Unknown tools default to WRITE. A profile without an approval channel denies approval-required work.

## Configuration

levels overrides individual tool classifications. blocked adds dangerous regular expressions and sensitive adds EXEC patterns with reasons. defaultLevel selects the unknown-tool level. monitor records ordinary classifications without interception; BLOCKED still denies execution. Invalid level overrides and invalid expressions are discarded.

index.js registers approval_classify and the tools/pre-execute listener through ctx.effect. src/classify.js owns classification; src/policy.js merges configuration. The profile bundle loads cordis.patch.yml and exposes the approvals service.

## Verification

Run npm ci and node --test tests/approvals.test.js. Unit classification evidence does not by itself prove a phone approval flow; that requires a real Host with an approval channel.
