---
name: manage-bw-webapp
description: Inspect, develop, troubleshoot, or deploy the bwicarus Flask website and its pages, routes, templates, static assets, dashboard data, authentication, control panel, and Pi-hosted services. Use for bwicarus.space website work, webapp routes or UI, dashboard refreshes, Flask errors, nginx/static routing, or requests to publish a site change. Do not use for unrelated third-party sites.
---

# Manage the BW web application

Use the repository's current architecture and deployment classifier. The old Claude website skill contains historical VPS commands and is not an execution authority.

## Orient to the requested surface

1. Read the relevant sections of `../../../references/webapp-development.md` for routes, authentication, storage, and source ownership.
2. Read `../../../references/deployment-workflow.md` before planning or performing any deployment.
3. Identify the actual source and consumer before editing:
   - Flask application and registered subsystems: `../../../_server_deploy/`
   - Templates and shared static sources: follow the ownership documented for that page.
   - Generated dashboard data: use its generator; do not hand-edit generated JSON.
   - Runtime user data, credentials, and `.env`: never treat them as source files or expose them to an AI model.
4. For Reader, PWA, or extension work, also follow the Reader entry documents named in the repository `AGENTS.md`; this skill does not replace those contracts.

## Work by request type

- Explanation or diagnosis: inspect routes, logs, data ownership, and current service state without modifying or restarting anything.
- Backend change: trace route registration, authentication, data boundary, failure behavior, and the relevant tests.
- UI or static change: confirm whether nginx or Flask serves the asset and preserve cache-bust behavior where applicable.
- Dashboard refresh: run the current scripted sequence; do not bypass Anki/status prerequisites or overwrite generated data manually.
- New route: update every required registration, protection, navigation, and serving layer proven by the current architecture.

## Validate

- Test the narrow changed behavior, authentication boundary, and failure path.
- For static/UI changes, verify the actually served asset, not a stale Flask or browser cache copy.
- Do not claim a user-visible interaction passed from server tests alone.
- Keep unrelated services, user data, and the paused VPS outside scope.

## Classify and deploy

Query `scripts/reader_deploy_manifest.py` for every changed path. Follow the A/B route in `../../../references/deployment-workflow.md`; do not infer the route from the directory and do not use manual VPS `scp` instructions from old documents.

Deployment, service restart, nginx change, or production write requires authorization from the current request. After an authorized deployment, report the atomic workflow result or the manual backup, service health, and rollback state as applicable.

