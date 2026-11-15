#!/usr/bin/env python3
"""External TestFlight for TwinKeys Remote: make the external group with a public link, add a build,
set "What to Test", and submit the build for TestFlight (beta) App Review.

App Store Connect API (Apple): apps, betaGroups, builds, preReleaseVersions, betaBuildLocalizations,
betaAppLocalizations, betaAppReviewDetails, betaAppReviewSubmissions.
https://developer.apple.com/documentation/appstoreconnectapi

Nothing personal is filled in here: if Test Information (feedback email, contact name, phone, email)
is missing, the script says which field and stops before submitting.

Usage: asc_beta.py <bundle id> <version> <build number> <group name>
Environment: ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_P8_BASE64 (as for the TestFlight upload).
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.appstoreconnect.apple.com"

WHATS_NEW = {
    "ja": (
        "1.2.0 の新機能を試してください。\n"
        "・Apple Watch：GO・シーンを選ぶ・全ノートオフ（iPhone の TwinKeys Remote を開いたまま使います）\n"
        "・メインの Mac が止まったとき、iPhone からバックアップの Mac に切り替え（1 秒長押し）\n"
        "・上の帯の Apple Watch の印と、設定の「接続」\n"
        "・Mac がなくても、最初の画面の「サンプルで試す」で全部の画面を試せます\n"
        "気づいたことは TestFlight の「フィードバックを送信」から送ってください。"
    ),
    "en-US": (
        "Please try what is new in 1.2.0.\n"
        "- Apple Watch: GO, choose scene, All Notes Off (keep TwinKeys Remote open on the iPhone)\n"
        "- When the main Mac stops, switch to the backup Mac from the iPhone (hold 1 second)\n"
        "- The Apple Watch mark in the top bar and Settings > Connection\n"
        "- No Mac? Tap \"Try with sample data\" on the first screen to try every screen\n"
        "Send feedback with \"Send Beta Feedback\" in TestFlight."
    ),
}

REVIEW_NOTES = (
    "TwinKeys Remote is a remote control for TwinKeys, a free, open-source Mac app "
    "(https://github.com/kajisho5/ableton-twinkeys) that controls Ableton Live on a main and a backup Mac "
    "over the local network. Without a Mac, tap \"Try with sample data\" on the first screen: every screen, "
    "including the Apple Watch app, then works with fictional data and nothing is sent anywhere. "
    "No account and no sign-in."
)


def die(msg, code=1):
    print(f"::error::{msg}")
    sys.exit(code)


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"{name} is empty (repository secret missing?)")
    return value


def p8_pem():
    raw = env("ASC_KEY_P8_BASE64")
    pem = raw if "BEGIN PRIVATE KEY" in raw else base64.b64decode("".join(raw.split())).decode()
    if "BEGIN PRIVATE KEY" not in pem:
        die("ASC_KEY_P8_BASE64 is neither PEM nor base64 of a PEM")
    return pem.replace("\r\n", "\n").strip() + "\n"


def token():
    import jwt  # PyJWT

    now = int(time.time())
    return jwt.encode(
        {"iss": env("ASC_ISSUER_ID"), "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
        p8_pem(),
        algorithm="ES256",
        headers={"kid": env("ASC_KEY_ID"), "typ": "JWT"},
    )


def api(method, path, body=None, ok=(200, 201, 204)):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code in ok:
            return e.code, {}
        print(f"{method} {path.split('?')[0]} -> HTTP {e.code}")
        print(text[:4000])
        return e.code, {"_error": text}


def q(params):
    return "?" + urllib.parse.urlencode(params)


def get(path, params=None):
    status, body = api("GET", path + (q(params) if params else ""))
    if status != 200:
        die(f"GET {path} failed (HTTP {status})")
    return body


def main():
    if len(sys.argv) != 5:
        die("usage: asc_beta.py <bundle id> <version> <build number> <group name>")
    bundle_id, version, build_number, group_name = sys.argv[1:]

    apps = get("/v1/apps", {"filter[bundleId]": bundle_id})["data"]
    if not apps:
        die(f"No App Store Connect app for {bundle_id}")
    app_id = apps[0]["id"]
    print(f"App: {apps[0]['attributes'].get('name')} ({app_id})")

    # 1. External group with a public link (Apple: an internal group must exist first; it does, builds 1-5 were tested).
    groups = get("/v1/betaGroups", {"filter[app]": app_id, "limit": "200"})["data"]
    group = next((g for g in groups if g["attributes"]["name"] == group_name and not g["attributes"].get("isInternalGroup")), None)
    if group is None:
        status, body = api("POST", "/v1/betaGroups", {
            "data": {
                "type": "betaGroups",
                "attributes": {
                    "name": group_name,
                    "isInternalGroup": False,
                    "publicLinkEnabled": True,
                    "publicLinkLimitEnabled": False,
                    "feedbackEnabled": True,
                },
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        })
        if status != 201:
            die("Could not create the external group")
        group = body["data"]
        print(f"Created external group '{group_name}' ({group['id']})")
    else:
        print(f"External group '{group_name}' exists ({group['id']})")
        if not group["attributes"].get("publicLinkEnabled"):
            api("PATCH", f"/v1/betaGroups/{group['id']}", {
                "data": {"type": "betaGroups", "id": group["id"], "attributes": {"publicLinkEnabled": True, "publicLinkLimitEnabled": False}}
            })
            print("Turned the public link on")

    # 2. The build, once Apple has processed it.
    build = None
    for attempt in range(60):
        builds = get("/v1/builds", {
            "filter[app]": app_id,
            "filter[version]": build_number,
            "filter[preReleaseVersion.version]": version,
            "filter[preReleaseVersion.platform]": "IOS",
        })["data"]
        if builds:
            build = builds[0]
            state = build["attributes"].get("processingState")
            print(f"Build {version} ({build_number}): {state}")
            if state == "VALID":
                break
            if state in ("FAILED", "INVALID"):
                die(f"Apple could not process build {build_number}: {state}")
        else:
            print(f"Build {version} ({build_number}) not listed yet")
        time.sleep(30)
    else:
        die("The build was not processed within 30 minutes; run this again later")
    build_id = build["id"]

    # 3. What to Test (ja and en-US).
    locs = get(f"/v1/builds/{build_id}/betaBuildLocalizations")["data"]
    for locale, text in WHATS_NEW.items():
        existing = next((l for l in locs if l["attributes"]["locale"] == locale), None)
        if existing:
            api("PATCH", f"/v1/betaBuildLocalizations/{existing['id']}", {
                "data": {"type": "betaBuildLocalizations", "id": existing["id"], "attributes": {"whatsNew": text}}
            })
        else:
            api("POST", "/v1/betaBuildLocalizations", {
                "data": {
                    "type": "betaBuildLocalizations",
                    "attributes": {"locale": locale, "whatsNew": text},
                    "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
                }
            })
        print(f"What to Test set ({locale})")

    # 4. Add the build to the group.
    status, _ = api("POST", f"/v1/betaGroups/{group['id']}/relationships/builds", {"data": [{"type": "builds", "id": build_id}]})
    print("Build added to the group" if status in (200, 201, 204) else f"Adding the build returned HTTP {status}")

    # 5. Test Information that the review needs. Personal fields are never filled in here.
    missing = []
    app_locs = get(f"/v1/apps/{app_id}/betaAppLocalizations")["data"]
    if not app_locs:
        missing.append("Test Information > Beta App Description (and Feedback Email)")
    for l in app_locs:
        a = l["attributes"]
        if not a.get("description"):
            missing.append(f"Beta App Description ({a.get('locale')})")
        if not a.get("feedbackEmail"):
            missing.append(f"Feedback Email ({a.get('locale')})")
    detail = get(f"/v1/apps/{app_id}/betaAppReviewDetail")["data"]
    d = detail["attributes"]
    for field, label in (("contactFirstName", "First name"), ("contactLastName", "Last name"),
                         ("contactPhone", "Phone"), ("contactEmail", "Email")):
        if not d.get(field):
            missing.append(f"Beta App Review Information > {label}")
    if not d.get("notes") or d.get("demoAccountRequired") is None:
        api("PATCH", f"/v1/betaAppReviewDetails/{detail['id']}", {
            "data": {"type": "betaAppReviewDetails", "id": detail["id"],
                     "attributes": {"notes": d.get("notes") or REVIEW_NOTES, "demoAccountRequired": False}}
        })
        print("Review notes set (how to try the app without a Mac)")
    if missing:
        print("Missing in App Store Connect (TestFlight > Test Information):")
        for m in missing:
            print(f"  - {m}")
        die("Fill these in, then run this again to submit the build for review", 2)

    # 6. Submit for TestFlight App Review (only the first build of a version needs a full review).
    status, body = api("GET", f"/v1/builds/{build_id}/betaAppReviewSubmission")
    review = (body.get("data") or {}).get("attributes", {}).get("betaReviewState") if status == 200 else None
    if review:
        print(f"Already submitted: {review}")
    else:
        status, body = api("POST", "/v1/betaAppReviewSubmissions", {
            "data": {"type": "betaAppReviewSubmissions", "relationships": {"build": {"data": {"type": "builds", "id": build_id}}}}
        })
        if status != 201:
            die("Submitting for TestFlight App Review failed (see above)")
        print(f"Submitted for TestFlight App Review: {body['data']['attributes'].get('betaReviewState')}")

    # 7. The public link (testers can join once the review has passed).
    g = get(f"/v1/betaGroups/{group['id']}")["data"]["attributes"]
    print(f"Public link: {g.get('publicLink') or '(appears once Apple enables it)'}")


if __name__ == "__main__":
    main()
