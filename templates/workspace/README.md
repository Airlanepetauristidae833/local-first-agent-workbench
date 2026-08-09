# Workspace Template

This directory demonstrates the generic workspace registry. It is project-neutral and
read-only inside the API container.

To register a project:

1. Create one direct child directory under the host path configured by `WORKSPACES_HOST_ROOT`.
2. Copy `.ai-workspace.json` into the new directory.
3. Change `id` to the exact lowercase directory name.
4. Set a human-readable `name` and `description`.
5. Keep command execution disabled unless a separately reviewed workflow explicitly needs it.
6. Mount or synchronize only the project material that the agent is allowed to inspect.

The authoritative project notes should remain in the configured knowledge vault. A registered
workspace is a disposable, read-only mirror for authenticated inspection and bounded search.

The API can inspect file metadata and perform bounded literal searches across an explicit
text-extension allowlist. Search results contain short matching-line excerpts only. The default
workspace policy does not permit arbitrary file retrieval, modification, or command execution.
