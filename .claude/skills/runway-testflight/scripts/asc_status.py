#!/usr/bin/env python3
"""Print the TestFlight state of the latest builds (no secrets are printed).

Env: ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_P8_BASE64 (PEM or base64), BUNDLE_ID.
"""
import base64
import json
import os
import time
import urllib.parse
import urllib.request

import jwt

API = "https://api.appstoreconnect.apple.com"


def pem():
    raw = os.environ["ASC_KEY_P8_BASE64"]
    if "BEGIN PRIVATE KEY" not in raw:
        raw = base64.b64decode("".join(raw.split())).decode()
    return raw.replace("\r\n", "\n").strip() + "\n"


def get(path, **query):
    now = int(time.time())
    token = jwt.encode({"iss": os.environ["ASC_ISSUER_ID"].strip(), "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
                       pem(), algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"].strip(), "typ": "JWT"})
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        print(f"GET {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:1500]}")
        return {}


def main():
    bundle_id = os.environ["BUNDLE_ID"]
    apps = get("/v1/apps", **{"filter[bundleId]": bundle_id}).get("data", [])
    if not apps:
        raise SystemExit(f"No App Store Connect app for {bundle_id}")
    app = apps[0]
    print(f"App: {app['attributes'].get('name')} ({app['id']})")

    builds = get("/v1/builds", **{"filter[app]": app["id"], "sort": "-uploadedDate", "limit": "5",
                                  "include": "buildBetaDetail,preReleaseVersion"})
    included = {(i["type"], i["id"]): i for i in builds.get("included", [])}
    for b in builds.get("data", []):
        a = b["attributes"]
        rel = b.get("relationships", {})
        detail = included.get(("buildBetaDetails", (rel.get("buildBetaDetail", {}).get("data") or {}).get("id")), {}).get("attributes", {})
        pre = included.get(("preReleaseVersions", (rel.get("preReleaseVersion", {}).get("data") or {}).get("id")), {}).get("attributes", {})
        print(f"\nBuild {pre.get('version')} ({a.get('version')}) uploaded {a.get('uploadedDate')}")
        print(f"  processingState: {a.get('processingState')}  expired: {a.get('expired')}")
        print(f"  usesNonExemptEncryption: {a.get('usesNonExemptEncryption')}")
        print(f"  internalBuildState: {detail.get('internalBuildState')}  externalBuildState: {detail.get('externalBuildState')}")

    print("\nBeta groups of the app:")
    for g in get(f"/v1/apps/{app['id']}/betaGroups").get("data", []):
        ga = g["attributes"]
        testers = get(f"/v1/betaGroups/{g['id']}/betaTesters", limit="50").get("data", [])
        print(f"  {ga.get('name')}: internal={ga.get('isInternalGroup')} allBuilds={ga.get('hasAccessToAllBuilds')} testers={len(testers)}")
        for t in testers:
            ta = t["attributes"]
            # state: e.g. INVITED / ACCEPTED / INSTALLED (no email printed)
            print(f"    tester {t['id'][:8]}…: inviteType={ta.get('inviteType')} state={ta.get('state')}")


if __name__ == "__main__":
    main()
