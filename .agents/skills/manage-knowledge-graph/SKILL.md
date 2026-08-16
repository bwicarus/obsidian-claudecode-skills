---
name: manage-knowledge-graph
description: Inspect, explain, repair, or extend this project's skill-tree and knowledge-graph subsystem, including nodes, links, mastery, unlock rules, rejected links, PDF scans, archive behavior, the skill-tree UI, and its Flask APIs. Use when the user mentions 技能树, 知识图谱, KG, nodes, mastery, unlocks, graph linking, skilltree.py, or the /skilltree pages.
---

# Manage the knowledge graph

Use current source and the dedicated system reference. Do not copy deployment commands from the old Claude skill.

## Establish the current model

1. Read `../../../references/skill-tree-system.md` completely before changing graph semantics, storage, APIs, or UI.
2. Inspect the live source relevant to the request:
   - Backend and routes: `../../../_server_deploy/skilltree.py`
   - UI: `../../../_server_deploy/templates/skilltree.html`
   - Graph algorithms and maintenance: `../../../scripts/kg/`
   - Persistent graph data: `knowledge_graph/` on the environment that owns it. The Windows checkout may not contain production graph data; absence there is not evidence that the graph was deleted.
3. Treat current code and persisted schema as authority over line numbers or commands in `.claude/skills/技能树.md`.

## Choose the operation

- For explanation or diagnosis, remain read-only and trace the specific node, route, job, or state transition.
- For an algorithm or UI change, keep graph semantics, API behavior, and UI behavior in the same acceptance model; do not infer success from one layer alone.
- For a graph-data mutation, resolve the exact book and operation first. Preserve a recoverable copy and never bulk-delete, merge, archive, or rewrite a graph without explicit authorization.
- Before running a maintenance script, inspect its current `--help`, whether it writes in place, and which AI CLI it invokes. Several KG scripts are Pi-oriented and may hard-code a Claude executable; do not assume they run on Windows merely because the Python file imports.

## Validate

- Validate JSON structure and the affected semantic invariant, such as link direction, mastery propagation, rejected-link behavior, or archive identity.
- Run the smallest relevant unit or contract tests plus any script-provided audit for the affected graph.
- For UI behavior, verify the route and the rendered interaction; a Python import or JSON parse is not visual acceptance.

## Deploy safely

Read `../../../references/deployment-workflow.md` before any deployment. Query `scripts/reader_deploy_manifest.py` for every changed path and follow the resulting route. The manifest and deployment document override all manual `cp`, VPS, or restart instructions in older skill and reference text.

Do not deploy, restart services, or alter production graph data unless the current user request authorizes that external effect.
