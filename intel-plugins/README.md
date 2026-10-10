# Intel plugins

English | [中文](README.zh.md)

Intel plugins add Muse capabilities to the DSH Host through the existing plugin interfaces. HMC accesses them through its service bridge. The DSH baseline is recorded in ../dsh-baseline.json.

Install each plugin's locked dependencies with npm ci before adding its directory to a DSH profile. Ignored node_modules directories are not supplied by a clone. Plugin tools and listeners use ctx.effect so unloading removes registrations. Credentials remain outside Git.

The cron plugin supplies a Schedule overlay, the approvals plugin classifies tool execution, and the artifacts, browser, Feed, Goals, memory, profile, Soul, Hooks and upkeep plugins expose their own tools and data. Each plugin README owns its configuration, operational limits and test commands.

Cron, Goals, memory tools and system-event tools throw supported HarnessError failures with stable codes. The executor records isError=true and error.info.code; the model and default user presentation receive fixed safe text, excluding raw exceptions. JSON errno and committed state appear in that text only for owner-created persistence failures; they are not additional ToolErrorInfo fields. Successful output values, schemas and memoryWrite metadata retain their existing fields. Other intel tools keep their owning semantics; a successful empty search is not an operation failure.
