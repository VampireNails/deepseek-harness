# dsh-intelligence-artifacts

English | [中文](README.zh.md)

artifact_save stores a titled Markdown, HTML or text artifact. Reusing a title appends a version under the same ID. artifact_get reads a selected or latest version, artifact_versions lists versions, and artifact_read reads the latest content. Invalid kinds fall back to Markdown.

## Storage and presentation

Artifacts live under $DSH_HOME/intel-artifacts, defaulting to ~/.dsh/intel-artifacts. Each artifact has meta.json and versioned content files. Reads accept the earlier single-file format as version 1; a subsequent save retains that content as the first version.

Publishing tools validate optional `references` against existing memory IDs before saving. The Linux [task runner](../../intel/task-runner/README.md) attaches its task, run and date identity; each version retains its own identity and references. Repeating a publication returns its existing version, while a conflicting title, content, kind or identity is rejected. Store writes are serialized across processes and metadata is replaced atomically. Manual saves still append versions by title and do not invent a runner identity.

The plugin supplies tools and metadata, without HTTP rendering routes. Its /artifacts/{id} URL is a presentation convention, not evidence that a server serves that address. HMC obtains artifacts through its authenticated bridge and owns display and export behavior.

## Installation

Run `npm ci` in the plugin directory, then `dsh plugin --profile <name> add link:/absolute/path/to/dsh-fork/intel-plugins/dsh-intelligence-artifacts`. The profile links the repository source so the sibling shared publication module remains available. Copying the plugin directory alone is insufficient. Dependencies are pinned to the DSH baseline; do not substitute registry default versions.
