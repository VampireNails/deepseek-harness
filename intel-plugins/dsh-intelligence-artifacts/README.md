# dsh-intelligence-artifacts

English | [中文](README.zh.md)

artifact_save stores a titled Markdown, HTML or text artifact. Reusing a title appends a version under the same ID. artifact_get reads a selected or latest version, artifact_versions lists versions, and artifact_read reads the latest content. Invalid kinds fall back to Markdown.

## Storage and presentation

Artifacts live under $DSH_HOME/intel-artifacts, defaulting to ~/.dsh/intel-artifacts. Each artifact has meta.json and versioned content files. Reads accept the earlier single-file format as version 1; a subsequent save retains that content as the first version.

The plugin supplies tools and metadata, without HTTP rendering routes. Its /artifacts/{id} URL is a presentation convention, not evidence that a server serves that address. HMC obtains artifacts through its authenticated bridge and owns display and export behavior.

## Installation

Run `npm ci` in the plugin directory, then `dsh plugin --profile <name> add .`. Dependencies are pinned to the DSH baseline; do not substitute registry default versions.
