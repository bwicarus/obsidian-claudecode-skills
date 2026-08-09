#!/usr/bin/env python3
"""Read-only App Store Connect/TestFlight build status.

This intentionally performs only GET requests.  It is used by the iOS release
workflow to distinguish Transporter acceptance from a build that testers can
actually install.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import jwt


API_ROOT = "https://api.appstoreconnect.apple.com"


def api_get(token: str, path: str, params: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_ROOT}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"App Store Connect GET {path} failed (HTTP {exc.code}): {payload}") from exc


def relationship_ids(resource: dict[str, Any], name: str) -> list[str]:
    data = resource.get("relationships", {}).get(name, {}).get("data")
    if isinstance(data, dict) and data.get("id"):
        return [str(data["id"])]
    if isinstance(data, list):
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]
    return []


def included_index(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("type")), str(item.get("id"))): item
        for item in payload.get("included", [])
        if isinstance(item, dict) and item.get("type") and item.get("id")
    }


def select_build(resources: list[dict[str, Any]], build_number: str | None) -> dict[str, Any] | None:
    if build_number:
        for resource in resources:
            if str(resource.get("attributes", {}).get("version", "")) == build_number:
                return resource
        return None
    return resources[0] if resources else None


def write_github_outputs(summary: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    latest = summary.get("build") or {}
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"found={'true' if latest else 'false'}\n")
        if latest:
            for key in (
                "build_number",
                "processing_state",
                "internal_build_state",
                "external_build_state",
                "uploaded_date",
            ):
                output.write(f"{key}={latest.get(key, '')}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--version", required=True, help="CFBundleShortVersionString, for example 1.1.8")
    parser.add_argument("--build-number", default="", help="Optional CFBundleVersion")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--issuer-id", required=True)
    parser.add_argument("--private-key", required=True)
    args = parser.parse_args()

    now = int(time.time())
    private_key = Path(args.private_key).read_text(encoding="utf-8")
    token = jwt.encode(
        {"iss": args.issuer_id, "iat": now, "exp": now + 15 * 60, "aud": "appstoreconnect-v1"},
        private_key,
        algorithm="ES256",
        headers={"kid": args.key_id, "typ": "JWT"},
    )

    apps = api_get(token, "/v1/apps", {"filter[bundleId]": args.bundle_id, "limit": "1"})
    app_resources = apps.get("data", [])
    if not app_resources:
        raise RuntimeError(f"App Store Connect app not found for bundle ID {args.bundle_id}")
    app_id = str(app_resources[0]["id"])

    upload_params = {
        "filter[cfBundleShortVersionString]": args.version,
        "filter[platform]": "IOS",
        "include": "build",
        "limit": "20",
        "sort": "-uploadedDate",
    }
    if args.build_number:
        upload_params["filter[cfBundleVersion]"] = args.build_number
    uploads = api_get(token, f"/v1/apps/{app_id}/buildUploads", upload_params)
    upload_rows = []
    for upload in uploads.get("data", []):
        attrs = upload.get("attributes", {})
        upload_rows.append(
            {
                "build_number": attrs.get("cfBundleVersion"),
                "version": attrs.get("cfBundleShortVersionString"),
                "state": attrs.get("state"),
                "created_date": attrs.get("createdDate"),
                "uploaded_date": attrs.get("uploadedDate"),
                "processed_build_id": (relationship_ids(upload, "build") or [None])[0],
            }
        )

    app_groups_payload = api_get(
        token,
        f"/v1/apps/{app_id}/betaGroups",
        {
            "fields[betaGroups]": (
                "name,isInternalGroup,hasAccessToAllBuilds,publicLinkEnabled,"
                "publicLinkLimitEnabled,publicLinkLimit"
            ),
            "limit": "200",
        },
    )
    app_groups = []
    for group in app_groups_payload.get("data", []):
        attrs = group.get("attributes", {})
        app_groups.append(
            {
                "id": group.get("id"),
                "name": attrs.get("name"),
                "internal": attrs.get("isInternalGroup"),
                "all_builds": attrs.get("hasAccessToAllBuilds"),
                "public_link_enabled": attrs.get("publicLinkEnabled"),
                "public_link_limit_enabled": attrs.get("publicLinkLimitEnabled"),
                "public_link_limit": attrs.get("publicLinkLimit"),
            }
        )

    builds = api_get(
        token,
        "/v1/builds",
        {
            "filter[app]": app_id,
            "filter[preReleaseVersion.version]": args.version,
            "filter[preReleaseVersion.platform]": "IOS",
            "include": "preReleaseVersion,buildBetaDetail,betaGroups",
            "fields[buildBetaDetails]": "autoNotifyEnabled,internalBuildState,externalBuildState",
            "fields[betaGroups]": "name,isInternalGroup,hasAccessToAllBuilds,publicLinkEnabled",
            "limit": "20",
            "limit[betaGroups]": "50",
            "sort": "-uploadedDate",
        },
    )
    build = select_build(builds.get("data", []), args.build_number or None)
    build_summary: dict[str, Any] | None = None
    if build:
        included = included_index(builds)
        detail_ids = relationship_ids(build, "buildBetaDetail")
        detail = included.get(("buildBetaDetails", detail_ids[0])) if detail_ids else None
        detail_attrs = detail.get("attributes", {}) if detail else {}
        group_ids = relationship_ids(build, "betaGroups")
        groups = []
        for group_id in group_ids:
            group = included.get(("betaGroups", group_id), {})
            attrs = group.get("attributes", {})
            groups.append(
                {
                    "id": group_id,
                    "name": attrs.get("name"),
                    "internal": attrs.get("isInternalGroup"),
                    "all_builds": attrs.get("hasAccessToAllBuilds"),
                }
            )
        attrs = build.get("attributes", {})
        build_summary = {
            "id": build.get("id"),
            "version": args.version,
            "build_number": attrs.get("version"),
            "processing_state": attrs.get("processingState"),
            "uploaded_date": attrs.get("uploadedDate"),
            "expired": attrs.get("expired"),
            "uses_non_exempt_encryption": attrs.get("usesNonExemptEncryption"),
            "internal_build_state": detail_attrs.get("internalBuildState"),
            "external_build_state": detail_attrs.get("externalBuildState"),
            "beta_groups": groups,
        }

    summary = {
        "bundle_id": args.bundle_id,
        "version": args.version,
        "requested_build_number": args.build_number or None,
        "app_beta_groups": app_groups,
        "build_uploads": upload_rows,
        "build": build_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    write_github_outputs(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
