#!/usr/bin/env python3
"""Inspect or provision only StocksNative, including its Apple sign-in capability."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt

BUNDLE_ID = "space.bwicarus.stocksnative"
API_ROOT = "https://api.appstoreconnect.apple.com"


class AppleAPI:
    def __init__(self, key: Path, key_id: str, issuer_id: str):
        self.key = key.read_text(encoding="utf-8")
        self.key_id = key_id
        self.issuer_id = issuer_id

    def request(self, method: str, path: str, payload=None):
        if method not in {"GET", "POST"}:
            raise ValueError("This tool does not modify or delete existing resources")
        if method == "POST" and path not in {"/v1/bundleIds", "/v1/profiles", "/v1/bundleIdCapabilities"}:
            raise ValueError("Only the StocksNative bundle, Apple sign-in capability, or profile may be created")
        now = int(time.time())
        token = jwt.encode(
            {"iss": self.issuer_id, "iat": now - 30, "exp": now + 600,
             "aud": "appstoreconnect-v1"},
            self.key, algorithm="ES256", headers={"kid": self.key_id, "typ": "JWT"},
        )
        url = API_ROOT + path
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Never print response bodies; profile and certificate contents are unnecessary.
            raise RuntimeError(f"Apple {method} {path.split('?')[0]}: HTTP {error.code}") from None

    def listing(self, path: str, **params):
        query = urllib.parse.urlencode(params)
        next_path = path + ("?" + query if query else "")
        result = []
        while next_path:
            page = self.request("GET", next_path)
            result.extend(page.get("data", []))
            next_url = page.get("links", {}).get("next")
            if next_url and not next_url.startswith(API_ROOT + "/"):
                raise RuntimeError("Unexpected pagination host")
            next_path = next_url.removeprefix(API_ROOT) if next_url else None
        return result


def github_output(**values):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if "\n" in str(value) or "\r" in str(value):
                    raise ValueError("Invalid multiline output")
                stream.write(f"{key}={value}\n")


def identities(keychain: str):
    result = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning", keychain],
        check=True, capture_output=True, text=True,
    )
    return set(re.findall(r'\b([0-9A-F]{40})\s+"Apple Distribution:', result.stdout))


def install_profile(profile, destination: Path, team: str, fingerprints: set[str]):
    raw = base64.b64decode(profile["attributes"]["profileContent"], validate=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    destination.chmod(0o600)
    decoded = subprocess.run(
        ["security", "cms", "-D", "-i", str(destination)],
        check=True, capture_output=True,
    ).stdout
    data = plistlib.loads(decoded)
    if data.get("TeamIdentifier") != [team]:
        raise RuntimeError("Profile team does not match APPLE_TEAM_ID")
    if data.get("Entitlements", {}).get("application-identifier") != f"{team}.{BUNDLE_ID}":
        raise RuntimeError("Profile does not belong to StocksNative")
    if data.get("Entitlements", {}).get("com.apple.developer.applesignin") != ["Default"]:
        raise RuntimeError("Profile does not include Sign in with Apple")
    if data.get("Entitlements", {}).get("get-task-allow"):
        raise RuntimeError("Development profile cannot be used for distribution")
    if data.get("ProvisionedDevices") or data.get("ProvisionsAllDevices"):
        raise RuntimeError("Expected an App Store profile, not ad-hoc/enterprise")
    available = {hashlib.sha1(cert).hexdigest().upper() for cert in data.get("DeveloperCertificates", [])}
    if not available.intersection(fingerprints):
        raise RuntimeError("Profile does not include the imported distribution certificate")
    installed = Path.home() / "Library/MobileDevice/Provisioning Profiles" / f"{data['UUID']}.mobileprovision"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(raw)
    installed.chmod(0o600)
    github_output(profile_uuid=data["UUID"], profile_name=data["Name"], profile_path=installed)
    return {"uuid": data["UUID"], "name": data["Name"], "bundle": BUNDLE_ID}


def profile_supports_apple(profile) -> bool:
    raw = base64.b64decode(profile["attributes"]["profileContent"], validate=True)
    with tempfile.NamedTemporaryFile(suffix=".mobileprovision") as temporary:
        temporary.write(raw)
        temporary.flush()
        decoded = subprocess.run(
            ["security", "cms", "-D", "-i", temporary.name],
            check=True, capture_output=True,
        ).stdout
    return plistlib.loads(decoded).get("Entitlements", {}).get("com.apple.developer.applesignin") == ["Default"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inspect", "provision", "require-app"])
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", default=os.environ.get("API_KEY_ID"), required=False)
    parser.add_argument("--issuer-id", default=os.environ.get("API_ISSUER_ID"), required=False)
    parser.add_argument("--team-id", default=os.environ.get("TEAM_ID"))
    parser.add_argument("--keychain")
    parser.add_argument("--profile-output", type=Path)
    args = parser.parse_args()
    if not args.key_id or not args.issuer_id:
        parser.error("Apple API key ID and issuer ID are required")
    api = AppleAPI(args.private_key, args.key_id, args.issuer_id)
    bundles = api.listing("/v1/bundleIds", **{"filter[identifier]": BUNDLE_ID, "limit": 200})
    apps = api.listing("/v1/apps", **{"filter[bundleId]": BUNDLE_ID, "limit": 200})
    summary = {"bundleIdentifier": BUNDLE_ID, "bundleExists": bool(bundles),
               "appRecordExists": bool(apps), "appId": apps[0]["id"] if apps else None}
    print(json.dumps(summary, ensure_ascii=False))
    if args.mode == "inspect":
        return
    if args.mode == "require-app":
        if not apps:
            raise SystemExit("Create the NEW StocksNative app record in App Store Connect before uploading. Bundle: " + BUNDLE_ID)
        github_output(app_id=apps[0]["id"])
        return
    if not args.keychain or not args.profile_output or not args.team_id:
        parser.error("provision requires --keychain, --profile-output and --team-id")
    fingerprints = identities(args.keychain)
    if not fingerprints:
        raise SystemExit("No valid imported Apple Distribution identity is available")
    certificates = api.listing("/v1/certificates", **{
        "filter[certificateType]": "DISTRIBUTION,IOS_DISTRIBUTION", "limit": 200,
    })
    matching = [cert for cert in certificates if hashlib.sha1(base64.b64decode(
        cert["attributes"]["certificateContent"])).hexdigest().upper() in fingerprints]
    if not matching:
        raise SystemExit("Imported distribution certificate is not present in this Apple team")
    certificate_id = matching[0]["id"]
    if bundles:
        bundle = bundles[0]
        if bundle["attributes"]["identifier"] != BUNDLE_ID:
            raise RuntimeError("Apple bundle filter returned an unexpected identifier")
    else:
        bundle = api.request("POST", "/v1/bundleIds", {"data": {
            "type": "bundleIds", "attributes": {
                "identifier": BUNDLE_ID, "name": "StocksNative", "platform": "IOS",
            },
        }})["data"]
        print("Registered only the StocksNative bundle identifier")
    capabilities = api.listing(f"/v1/bundleIds/{bundle['id']}/bundleIdCapabilities", limit=200)
    if not any(item.get("attributes", {}).get("capabilityType") == "APPLE_ID_AUTH" for item in capabilities):
        api.request("POST", "/v1/bundleIdCapabilities", {"data": {
            "type": "bundleIdCapabilities",
            "attributes": {"capabilityType": "APPLE_ID_AUTH"},
            "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bundle["id"]}}},
        }})
        print("Enabled Sign in with Apple only for StocksNative")
    profiles = api.listing(f"/v1/bundleIds/{bundle['id']}/profiles", limit=200)
    selected = None
    for profile in profiles:
        attr = profile["attributes"]
        if attr.get("profileType") != "IOS_APP_STORE" or attr.get("profileState") != "ACTIVE":
            continue
        linked = api.listing(f"/v1/profiles/{profile['id']}/certificates", limit=200)
        if any(cert["id"] == certificate_id for cert in linked):
            candidate = api.request("GET", f"/v1/profiles/{profile['id']}")["data"]
            if profile_supports_apple(candidate):
                selected = candidate
                break
    if selected is None:
        selected = api.request("POST", "/v1/profiles", {"data": {
            "type": "profiles", "attributes": {
                "name": "StocksNative App Store " + time.strftime("%Y%m%d-%H%M%S", time.gmtime()),
                "profileType": "IOS_APP_STORE",
            },
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle["id"]}},
                "certificates": {"data": [{"type": "certificates", "id": certificate_id}]},
            },
        }})["data"]
        print("Created only a StocksNative distribution profile")
    print(json.dumps(install_profile(selected, args.profile_output, args.team_id, fingerprints)))


if __name__ == "__main__":
    main()
