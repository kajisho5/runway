#!/usr/bin/env python3
"""Make sure the app has an internal TestFlight group that gets every build, and that the
App Store Connect account holder / admins are testers in it. Prints no emails or secrets.

Env: ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_P8_BASE64, BUNDLE_ID.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from asc_status import API, pem  # noqa: E402

import jwt  # noqa: E402

GROUP_NAME = "Internal"


def call(method, path, body=None, **query):
    now = int(time.time())
    token = jwt.encode({"iss": os.environ["ASC_ISSUER_ID"].strip(), "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
                       pem(), algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"].strip(), "typ": "JWT"})
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:1500]


bundle_id = os.environ["BUNDLE_ID"]
_, apps = call("GET", "/v1/apps", **{"filter[bundleId]": bundle_id})
app_id = apps["data"][0]["id"]

_, groups = call("GET", f"/v1/apps/{app_id}/betaGroups")
internal = [g for g in groups.get("data", []) if g["attributes"].get("isInternalGroup")]
if internal:
    group = internal[0]
    print(f"Internal group exists: {group['attributes'].get('name')}")
    if not group["attributes"].get("hasAccessToAllBuilds"):
        status, _ = call("PATCH", f"/v1/betaGroups/{group['id']}", {"data": {
            "type": "betaGroups", "id": group["id"], "attributes": {"hasAccessToAllBuilds": True}}})
        print(f"Enable automatic distribution: HTTP {status}")
else:
    status, res = call("POST", "/v1/betaGroups", {"data": {
        "type": "betaGroups",
        "attributes": {"name": GROUP_NAME, "isInternalGroup": True, "hasAccessToAllBuilds": True},
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})
    print(f"Create internal group '{GROUP_NAME}': HTTP {status}")
    if status >= 300:
        print(res)
        sys.exit(1)
    group = res["data"]

_, existing = call("GET", f"/v1/betaGroups/{group['id']}/betaTesters", limit="200")
existing_emails = {t["attributes"].get("email", "").lower() for t in existing.get("data", [])}

_, users = call("GET", "/v1/users", limit="200")
admins = [u for u in users.get("data", []) if {"ACCOUNT_HOLDER", "ADMIN"} & set(u["attributes"].get("roles", []))]
added = 0
for u in admins:
    a = u["attributes"]
    email = (a.get("username") or "").lower()
    if not email or email in existing_emails:
        continue
    status, res = call("POST", "/v1/betaTesters", {"data": {
        "type": "betaTesters",
        "attributes": {"email": email, "firstName": a.get("firstName") or "", "lastName": a.get("lastName") or ""},
        "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group["id"]}]}}}})
    print(f"Add tester (role {','.join(a.get('roles', []))}): HTTP {status}")
    if status >= 300:
        print(res)
    else:
        added += 1
print(f"Testers already in group: {len(existing_emails)}, added: {added}")

# Send (or re-send) the TestFlight invitation to testers who have not accepted yet.
_, current = call("GET", f"/v1/betaGroups/{group['id']}/betaTesters", limit="200")
for t in current.get("data", []):
    state = t["attributes"].get("state")
    if state in ("ACCEPTED", "INSTALLED"):
        continue
    status, res = call("POST", "/v1/betaTesterInvitations", {"data": {
        "type": "betaTesterInvitations",
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}},
                          "betaTester": {"data": {"type": "betaTesters", "id": t["id"]}}}}})
    print(f"Send invitation (state {state}): HTTP {status}")
    if status >= 300:
        print(res)
