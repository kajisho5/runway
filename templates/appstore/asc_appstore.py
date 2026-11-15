#!/usr/bin/env python3
"""Prepare TwinKeys Remote for App Review in App Store Connect, WITHOUT submitting it.

Everything it uploads comes from docs/appstore/listing.json. It can be run again at any time: each
step reads what App Store Connect already has and only changes what differs.

Steps (each one prints OK / skipped / failed; the exit code is non-zero only when a step failed):
  1. The app (by its App Store Connect id, checked against the bundle id).
  2. The App Store version (platform IOS) in an editable state: the version with this number, or the
     editable version renamed to it (a new app record starts with "1.0 Prepare for Submission"), or a
     new one. Copyright and release type (manual release) are set on it.
  3. App information per language (name, subtitle, privacy policy URL), categories, age rating answers.
  4. Version information per language (description, keywords, promotional text, support and marketing
     URLs; What's New only when the app already had a version on the App Store).
  5. Primary language and content rights (app level).
  6. Price: a price schedule with base territory JPN and the JPN price point of the requested price.
     Refused because the Paid Apps Agreement is not active: reported, and the other steps go on.
  7. Availability: all territories, only when the app has none yet (Apple requires it before review).
  8. App Review information: notes, no sign-in. Contact name, phone and email are copied from the
     TestFlight Beta App Review Information only into empty fields (their values are never printed).
  9. Screenshots: iPhone 6.3", iPhone 6.9", iPad 13" from the repository and Apple Watch from the CI
     artifact (--watch-dir), through the asset reservation, upload and commit flow (MD5 checksum).
 10. The build (waits for Apple's processing) attached to the version.
It never creates a review submission: "Add for Review" and "Submit for Review" stay manual, together
with the items listed at the end.

App Store Connect API (Apple): https://developer.apple.com/documentation/appstoreconnectapi
Uploading assets: https://developer.apple.com/documentation/appstoreconnectapi/uploading-assets-to-app-store-connect
Screenshot sizes: https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications

Usage:
  asc_appstore.py --check                    check listing.json (Apple's limits, screenshots) offline
  asc_appstore.py --dry-run [--watch-dir D]  print the requests it would send, against a made-up new app
  asc_appstore.py [--version 1.2.1] [--build 7] [--price-jpy 1000] [--watch-dir D]
Environment (real run): ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_P8_BASE64 (as for the TestFlight upload).
"""

import argparse
import base64
import datetime
import decimal
import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

API = "https://api.appstoreconnect.apple.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LISTING = os.path.join(ROOT, "docs", "appstore", "listing.json")

# Apple's limits, checked 2026-11-12:
#  name 2-30 characters, subtitle 30:
#    https://developer.apple.com/help/app-store-connect/reference/app-information/app-information
#  promotional text 170 characters, description 4000 characters (plain text), keywords 100 bytes with
#  each keyword longer than two characters, What's New 4000 characters, App Review notes 4000 bytes:
#    https://developer.apple.com/help/app-store-connect/reference/app-information/platform-version-information
LIMITS = {
    "name": ("chars", 30),
    "subtitle": ("chars", 30),
    "promotionalText": ("chars", 170),
    "description": ("chars", 4000),
    "keywords": ("bytes", 100),
    "whatsNew": ("chars", 4000),
}
NOTES_MAX_BYTES = 4000
APP_INFO_FIELDS = ("name", "subtitle", "privacyPolicyUrl")
VERSION_FIELDS = ("description", "keywords", "promotionalText", "supportUrl", "marketingUrl", "whatsNew")

# Accepted pixel sizes per ScreenshotDisplayType. Sizes: Apple's Screenshot specifications (above).
# Apple's API reference lists the enum values without sizes; the size-to-value pairing matches the
# table in fastlane deliver (deliver/lib/deliver/app_screenshot.rb): APP_IPHONE_61 = "iPhone with
# Dynamic Island (medium display)", APP_IPHONE_67 = "(large display)", APP_IPAD_PRO_3GEN_129 = 13".
# App Store Connect checks the dimensions itself when it processes each upload.
SIZES = {
    "APP_IPHONE_61": {(1179, 2556), (2556, 1179), (1206, 2622), (2622, 1206)},
    "APP_IPHONE_67": {(1260, 2736), (2736, 1260), (1290, 2796), (2796, 1290), (1320, 2868), (2868, 1320)},
    "APP_IPAD_PRO_3GEN_129": {(2064, 2752), (2752, 2064), (2048, 2732), (2732, 2048)},
}
# Apple Watch: one of these sizes, the same in every localization (Apple: "You must use the same
# screenshot size for Apple Watch consistently across all localizations for the app").
WATCH_TYPES = {
    (422, 514): "APP_WATCH_ULTRA",      # Ultra 4, Ultra 3
    (410, 502): "APP_WATCH_ULTRA",      # Ultra 2, Ultra
    (416, 496): "APP_WATCH_SERIES_10",  # Series 12, 11, 10
    (396, 484): "APP_WATCH_SERIES_7",   # Series 9, 8, 7
    (368, 448): "APP_WATCH_SERIES_4",   # Series 6, 5, 4, SE
    (312, 390): "APP_WATCH_SERIES_3",   # Series 3, 2, 1
}
ALL_WATCH_TYPES = sorted(set(WATCH_TYPES.values()))

# AppVersionState / AppStoreVersionState (Apple's enums). Editable: metadata can still be changed.
EDITABLE_VERSION = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED",
                    "INVALID_BINARY", "READY_FOR_REVIEW"}
# A version in one of these states has been on the App Store: the next version may have What's New.
ON_STORE = {"READY_FOR_DISTRIBUTION", "PROCESSING_FOR_DISTRIBUTION", "REPLACED_WITH_NEW_VERSION",
            "READY_FOR_SALE", "PROCESSING_FOR_APP_STORE", "PREORDER_READY_FOR_SALE", "REMOVED_FROM_SALE",
            "DEVELOPER_REMOVED_FROM_SALE"}
EDITABLE_APP_INFO = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "READY_FOR_REVIEW",
                     "METADATA_REJECTED", "INVALID_BINARY"}
CONTACT_FIELDS = (("contactFirstName", "First name"), ("contactLastName", "Last name"),
                  ("contactPhone", "Phone number"), ("contactEmail", "Email"))

ASC = "https://appstoreconnect.apple.com"


class Fatal(Exception):
    """A step failed in a way that makes the following steps pointless."""


# ---------------------------------------------------------------------------------------------------
# Output


class Report:
    def __init__(self):
        self.rows = []
        self.manual = []

    def add(self, status, step, detail=""):
        self.rows.append((status, step, detail))
        print(f"[{status}] {step}" + (f": {detail}" if detail else ""))

    def ok(self, step, detail=""):
        self.add("OK", step, detail)

    def skipped(self, step, detail=""):
        self.add("skipped", step, detail)

    def failed(self, step, detail=""):
        self.add("failed", step, detail)
        print(f"::error::{step}: {detail}")

    def todo(self, text):
        if text not in self.manual:
            self.manual.append(text)

    @property
    def any_failed(self):
        return any(r[0] == "failed" for r in self.rows)


REDACT = set()


def redact(text):
    for value in REDACT:
        if value and len(value) > 2:
            text = text.replace(value, "<redacted>")
    return text


def describe_body(body):
    """Attribute and relationship names of a request body, never the values (they may be personal)."""
    if not isinstance(body, dict) or "data" not in body:
        return ""
    data = body["data"]
    if isinstance(data, list):
        return f"[{len(data)} item(s)]"
    parts = []
    attrs = data.get("attributes") or {}
    if attrs:
        parts.append("attributes: " + ", ".join(sorted(attrs)))
    rels = data.get("relationships") or {}
    if rels:
        parts.append("relationships: " + ", ".join(sorted(rels)))
    if body.get("included"):
        parts.append(f"included: {len(body['included'])}")
    return "; ".join(parts)


def error_summary(text):
    try:
        errors = json.loads(text).get("errors") or []
    except (ValueError, AttributeError):
        return redact(text[:600])
    out = []
    for e in errors:
        bit = " ".join(x for x in (e.get("code"), "-", e.get("title"), "-", e.get("detail")) if x)
        pointer = (e.get("source") or {}).get("pointer")
        out.append(bit + (f" [{pointer}]" if pointer else ""))
    return redact("; ".join(out) or text[:600])


# ---------------------------------------------------------------------------------------------------
# App Store Connect API client


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise Fatal(f"{name} is empty (repository secret missing?)")
    return value


def p8_pem():
    raw = env("ASC_KEY_P8_BASE64")
    pem = raw if "BEGIN PRIVATE KEY" in raw else base64.b64decode("".join(raw.split())).decode()
    if "BEGIN PRIVATE KEY" not in pem:
        raise Fatal("ASC_KEY_P8_BASE64 is neither PEM nor base64 of a PEM")
    return pem.replace("\r\n", "\n").strip() + "\n"


class Api:
    dry = False

    def __init__(self):
        self._token = None
        self._token_time = 0

    def token(self):
        # Apple: tokens live at most 20 minutes; this one lives 10 and is renewed after 8.
        if self._token and time.time() - self._token_time < 480:
            return self._token
        import jwt  # PyJWT

        now = int(time.time())
        self._token = jwt.encode(
            {"iss": env("ASC_ISSUER_ID"), "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
            p8_pem(), algorithm="ES256", headers={"kid": env("ASC_KEY_ID"), "typ": "JWT"},
        )
        self._token_time = time.time()
        return self._token

    def request(self, method, path, body=None, params=None):
        """Returns (status, json body or {}, raw text). Never raises for HTTP errors."""
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if method != "GET":
            print(f"  -> {method} {urllib.parse.urlsplit(url).path} {describe_body(body)}".rstrip())
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=90) as res:
                    raw = res.read().decode(errors="replace")
                    return res.status, (json.loads(raw) if raw.strip() else {}), raw
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                retry = e.code == 429 or (e.code >= 500 and method != "POST")
                if retry and attempt < 4:
                    time.sleep(5 * (attempt + 1) ** 2)
                    continue
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    parsed = {}
                return e.code, parsed, raw
            except (urllib.error.URLError, TimeoutError) as e:
                if method in ("GET", "PATCH", "DELETE") and attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                return 0, {}, f"network error: {e}"
        return 0, {}, "gave up after retries"

    def upload_part(self, op, part):
        """One upload operation from an asset reservation (no JWT: the URL is pre-signed)."""
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
        last = ""
        for attempt in range(4):
            req = urllib.request.Request(op["url"], data=part, method=op.get("method", "PUT"), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=180) as res:
                    if 200 <= res.status < 300:
                        return True, ""
                    last = f"HTTP {res.status}"
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code} {e.read()[:300]!r}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"network error: {e}"
            time.sleep(5 * (attempt + 1))
        return False, last

    def sleep(self, seconds):
        time.sleep(seconds)

    # Helpers -----------------------------------------------------------------------------------

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def get_ok(self, path, params=None, what=None):
        status, body, raw = self.get(path, params)
        if status != 200:
            raise Fatal(f"GET {what or path} -> HTTP {status} {error_summary(raw)}")
        return body

    def get_all(self, path, params=None, what=None):
        body = self.get_ok(path, params, what)
        items = list(body.get("data") or [])
        included = list(body.get("included") or [])
        nxt = (body.get("links") or {}).get("next")
        pages = 0
        while nxt and pages < 50:
            body = self.get_ok(nxt, None, what)
            items += body.get("data") or []
            included += body.get("included") or []
            nxt = (body.get("links") or {}).get("next")
            pages += 1
        return items, included

    def write(self, method, path, body=None, ok=(200, 201, 204)):
        status, parsed, raw = self.request(method, path, body)
        if status not in ok:
            return False, status, parsed, error_summary(raw)
        return True, status, parsed, ""


# ---------------------------------------------------------------------------------------------------
# Dry run: the same steps against a made-up new app record (nothing is sent anywhere)


def preview(obj):
    """A request body with long texts shortened and long lists cut, for the dry run."""
    if isinstance(obj, str):
        return obj if len(obj) <= 70 else obj[:60].replace("\n", " ") + f"... ({len(obj)} chars)"
    if isinstance(obj, list):
        out = [preview(x) for x in obj[:3]]
        return out + ([f"... {len(obj) - 3} more"] if len(obj) > 3 else [])
    if isinstance(obj, dict):
        return {k: preview(v) for k, v in obj.items()}
    return obj


class DryApi(Api):
    dry = True

    def __init__(self, bundle_id):
        super().__init__()
        self.bundle_id = bundle_id
        self.n = 0
        self.counts = {}

    def token(self):
        return "dry-run"

    def sleep(self, seconds):
        pass

    def _id(self, kind):
        self.n += 1
        return f"dry-{kind}-{self.n}"

    def request(self, method, path, body=None, params=None):
        p = urllib.parse.urlsplit(path).path if path.startswith("http") else path
        if method != "GET":
            self.counts[method] = self.counts.get(method, 0) + 1
            print(f"  PLAN {method} {p}" + (f"\n       {json.dumps(preview(body), ensure_ascii=False)}" if body else ""))
            if method == "POST":
                data = dict(body["data"])
                data["id"] = self._id(data["type"])
                attrs = dict(data.get("attributes") or {})
                if data["type"] == "appScreenshots":
                    attrs["uploadOperations"] = [{"method": "PUT", "url": "https://upload.invalid/part1",
                                                  "offset": 0, "length": attrs["fileSize"],
                                                  "requestHeaders": [{"name": "Content-Type", "value": "image/png"}]}]
                data["attributes"] = attrs
                return 201, {"data": data}, ""
            if method == "PATCH" and isinstance(body.get("data"), dict):
                return 200, {"data": body["data"]}, ""
            return 204, {}, ""
        return self._fake_get(p, params or {})

    def _fake_get(self, p, params):
        def one(data):
            return 200, {"data": data}, ""
        if re.fullmatch(r"/v1/apps/\d+", p):
            return one({"id": p.split("/")[-1], "type": "apps", "attributes": {
                "bundleId": self.bundle_id, "name": "TwinKeys Remote", "primaryLocale": "ja",
                "contentRightsDeclaration": None}})
        if p.endswith("/appStoreVersions"):
            return one([{"id": "dry-version", "type": "appStoreVersions", "attributes": {
                "versionString": "1.0", "platform": "IOS", "appVersionState": "PREPARE_FOR_SUBMISSION",
                "appStoreState": "PREPARE_FOR_SUBMISSION", "copyright": None, "releaseType": None}}])
        if p.endswith("/appInfos"):
            return one([{"id": "dry-appinfo", "type": "appInfos", "attributes": {
                "state": "PREPARE_FOR_SUBMISSION", "appStoreAgeRating": None}, "relationships": {}}])
        if p.endswith("/appInfoLocalizations"):
            return one([{"id": "dry-appinfo-ja", "type": "appInfoLocalizations",
                         "attributes": {"locale": "ja", "name": "TwinKeys Remote"}}])
        if p.endswith("/ageRatingDeclaration"):
            return one({"id": "dry-age", "type": "ageRatingDeclarations", "attributes": {}})
        if p.endswith("/appStoreVersionLocalizations"):
            return one([{"id": "dry-version-ja", "type": "appStoreVersionLocalizations",
                         "attributes": {"locale": "ja"}}])
        if p.endswith("/appPriceSchedule") or p.endswith("/appAvailabilityV2"):
            return 404, {}, '{"errors":[{"code":"NOT_FOUND","title":"(dry run) none yet"}]}'
        if p.endswith("/appPricePoints"):
            return one([{"id": "dry-pp-980", "type": "appPricePoints", "attributes": {"customerPrice": "980.0"}},
                        {"id": "dry-pp-1000", "type": "appPricePoints", "attributes": {"customerPrice": "1000.0"}},
                        {"id": "dry-pp-1100", "type": "appPricePoints", "attributes": {"customerPrice": "1100.0"}}])
        if p == "/v1/territories":
            return one([{"id": t, "type": "territories"} for t in ("JPN", "USA", "DEU")])
        if p.endswith("/appStoreReviewDetail"):
            return 200, {"data": None}, ""
        if p.endswith("/betaAppReviewDetail"):
            return one({"id": "dry-beta", "type": "betaAppReviewDetails", "attributes": {
                "contactFirstName": "<first name from TestFlight>", "contactLastName": "<last name from TestFlight>",
                "contactPhone": "<phone from TestFlight>", "contactEmail": "<email from TestFlight>"}})
        if p.endswith("/appScreenshotSets") or p.endswith("/appScreenshots"):
            return one([])
        if re.fullmatch(r"/v1/appScreenshots/[^/]+", p):
            return one({"id": p.split("/")[-1], "type": "appScreenshots",
                        "attributes": {"assetDeliveryState": {"state": "COMPLETE", "errors": []}}})
        if p == "/v1/builds":
            return one([{"id": "dry-build", "type": "builds", "attributes": {
                "version": params.get("filter[version]"), "processingState": "VALID",
                "buildAudienceType": "APP_STORE_ELIGIBLE", "usesNonExemptEncryption": False}}])
        if p.endswith("/build"):
            return 200, {"data": None}, ""
        if p == "/v1/reviewSubmissions" or (p.startswith("/v1/reviewSubmissions/") and p.endswith("/items")):
            return one([])
        return 404, {}, f"(dry run) no fake answer for GET {p}"

    def upload_part(self, op, part):
        print(f"  PLAN {op.get('method', 'PUT')} <upload URL from the reservation> ({len(part)} bytes)")
        return True, ""


# ---------------------------------------------------------------------------------------------------
# PNG: size, alpha channel (Apple: "Images can't include alpha channels or transparencies")


def png_chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    i = 8
    while i + 8 <= len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        yield kind, data[i + 8:i + 8 + n]
        i += 12 + n
        if kind == b"IEND":
            break


def png_info(data):
    info = None
    trns = False
    for kind, body in png_chunks(data):
        if kind == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            info = {"width": w, "height": h, "depth": depth, "color": color, "interlace": interlace}
        elif kind == b"tRNS":
            trns = True
    if not info:
        raise ValueError("PNG without IHDR")
    info["alpha"] = info["color"] in (4, 6) or trns
    return info


def _png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png_without_alpha(data):
    """8-bit RGBA or gray+alpha, not interlaced -> RGB or gray, composited over black."""
    info = png_info(data)
    if info["color"] not in (4, 6) or info["depth"] != 8 or info["interlace"]:
        raise ValueError("only 8-bit, non-interlaced RGBA or gray+alpha PNGs can be converted here; "
                         "save it without transparency (RGB) instead")
    w, h = info["width"], info["height"]
    channels = 4 if info["color"] == 6 else 2
    keep = []
    idat = b""
    for kind, body in png_chunks(data):
        if kind == b"IDAT":
            idat += body
        elif kind in (b"sRGB", b"gAMA", b"cHRM", b"iCCP", b"pHYs"):
            keep.append((kind, body))
    raw = zlib.decompress(idat)
    stride = w * channels
    prev = bytearray(stride)
    out = bytearray()
    pos = 0
    bpp = channels
    for _ in range(h):
        f = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
        elif f != 0:
            raise ValueError(f"unknown PNG filter {f}")
        prev = bytearray(line)  # the next row is unfiltered against this row as decoded, with alpha
        out.append(0)
        alpha = line[channels - 1::channels]
        for ch in range(channels - 1):
            color = line[ch::channels]
            # Over black: c * a / 255 (fully opaque pixels keep their exact value).
            line[ch::channels] = bytes((c * a + 127) // 255 if a != 255 else c for c, a in zip(color, alpha))
        del line[channels - 1::channels]
        out += line
    new_color = 2 if channels == 4 else 0
    ihdr = struct.pack(">IIBBBBB", w, h, 8, new_color, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    for kind, body in keep:
        png += _png_chunk(kind, body)
    png += _png_chunk(b"IDAT", zlib.compress(bytes(out), 9)) + _png_chunk(b"IEND", b"")
    return png


def load_screenshot(path, accepted=None):
    """(file name, bytes to upload, (w, h), note). Raises ValueError with a readable reason."""
    if not os.path.isfile(path):
        raise ValueError(f"missing: {path}")
    with open(path, "rb") as f:
        data = f.read()
    info = png_info(data)
    size = (info["width"], info["height"])
    if accepted is not None and size not in accepted:
        raise ValueError(f"{os.path.basename(path)} is {size[0]}x{size[1]}; accepted: "
                         + ", ".join(f"{a}x{b}" for a, b in sorted(accepted)))
    note = ""
    if info["alpha"]:
        data = png_without_alpha(data)
        note = "alpha channel removed"
    return os.path.basename(path), data, size, note


# ---------------------------------------------------------------------------------------------------
# listing.json checks (offline)


def utf8_len(s):
    return len(s.encode("utf-8"))


MD_SOURCES = {
    "ja": ("docs/appstore/metadata-ja.md", {"name": 1, "subtitle": 2, "promotionalText": 3, "description": 4,
                                             "keywords": 5, "supportUrl": 6, "marketingUrl": 7,
                                             "privacyPolicyUrl": 8, "whatsNew": 12}, 11),
    "en-US": ("docs/appstore/metadata-en.md", {"name": 1, "subtitle": 2, "promotionalText": 3, "description": 4,
                                                "keywords": 5, "supportUrl": 6, "marketingUrl": 7,
                                                "privacyPolicyUrl": 8, "whatsNew": 10}, 9),
}


def md_blocks(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"^## (\d+)\.[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        b = re.search(r"```text\n(.*?)\n```", m.group(2), re.S)
        if b:
            out[int(m.group(1))] = b.group(1)
    return out


def check_listing(listing, watch_dir=None):
    """Prints counts; returns (errors, warnings)."""
    errors, warnings = [], []
    print("== Fields against Apple's limits")
    locs = listing.get("localizations") or {}
    for locale, fields in locs.items():
        for field, (unit, limit) in LIMITS.items():
            value = fields.get(field) or ""
            n = utf8_len(value) if unit == "bytes" else len(value)
            extra = f" ({utf8_len(value)} bytes)" if unit == "chars" and utf8_len(value) != len(value) else ""
            flag = "ok" if n <= limit else "TOO LONG"
            print(f"  {locale:6} {field:16} {n:5} / {limit} {unit}{extra}  {flag}")
            if n > limit:
                errors.append(f"{locale} {field}: {n} {unit}, limit {limit}")
        if len(fields.get("name") or "") < 2:
            errors.append(f"{locale} name: at least 2 characters")
        for field in ("name", "description", "keywords", "supportUrl", "privacyPolicyUrl"):
            if not fields.get(field):
                errors.append(f"{locale} {field} is empty (required)")
        for kw in (fields.get("keywords") or "").split(","):
            if len(kw.strip()) <= 2:
                errors.append(f"{locale} keyword '{kw}': Apple wants more than two characters")
        if re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", fields.get("description") or ""):
            errors.append(f"{locale} description looks like HTML (Apple: plain text only)")
        for field in ("supportUrl", "marketingUrl", "privacyPolicyUrl"):
            v = fields.get(field)
            if v and not re.fullmatch(r"https://[^\s]+", v):
                errors.append(f"{locale} {field} is not an https URL: {v}")
        if re.search(r"\bfree\b|無料", (fields.get("promotionalText") or "") + (fields.get("subtitle") or "")):
            warnings.append(f"{locale}: promotional text or subtitle says 'free' (the app is paid)")
        if re.search(r"TwinKeys Remote (is (a )?free\b|は無料)", fields.get("description") or ""):
            errors.append(f"{locale} description says TwinKeys Remote is free (it is a paid app)")
    notes = (listing.get("review") or {}).get("notes") or ""
    print(f"  review notes {utf8_len(notes):5} / {NOTES_MAX_BYTES} bytes  "
          + ("ok" if utf8_len(notes) <= NOTES_MAX_BYTES else "TOO LONG"))
    if utf8_len(notes) > NOTES_MAX_BYTES:
        errors.append(f"review notes: {utf8_len(notes)} bytes, limit {NOTES_MAX_BYTES}")
    copyright_ = (listing.get("version") or {}).get("copyright") or ""
    if not re.match(r"^\d{4} \S", copyright_):
        errors.append("copyright should be '<year> <owner>' (Apple adds the © sign)")
    if "©" in copyright_:
        errors.append("copyright: leave out the © sign (Apple adds it)")
    price = (listing.get("price") or {}).get("customerPrice", "")
    if not re.fullmatch(r"\d+", str(price)):
        errors.append(f"price.customerPrice should be whole yen, got {price!r}")

    # The same texts as in the metadata files people read (warning only).
    for locale, (rel, sections, copyright_section) in MD_SOURCES.items():
        path = os.path.join(ROOT, rel)
        if locale not in locs or not os.path.isfile(path):
            continue
        blocks = md_blocks(path)
        for field, num in sections.items():
            if blocks.get(num) is not None and blocks[num] != (locs[locale].get(field) or ""):
                warnings.append(f"{locale} {field} differs from {rel} section {num}")
        if blocks.get(copyright_section) not in (None, copyright_):
            warnings.append(f"copyright differs from {rel} section {copyright_section}")
    steps = os.path.join(ROOT, "docs/appstore/submit-steps.md")
    if os.path.isfile(steps):
        with open(steps, encoding="utf-8") as f:
            text = f.read()
        i = text.find("### 6-5.")
        m = re.search(r"```text\n(.*?)\n```", text[i:], re.S) if i >= 0 else None
        if m and m.group(1) != notes:
            warnings.append("review notes differ from submit-steps.md section 6-5")

    project = os.path.join(ROOT, "iOS/project.yml")
    if os.path.isfile(project):
        with open(project, encoding="utf-8") as f:
            m = re.search(r'MARKETING_VERSION:\s*"?([\d.]+)"?', f.read())
        version = (listing.get("version") or {}).get("versionString")
        if m and version and m.group(1) != version:
            warnings.append(f"listing version {version} but iOS/project.yml MARKETING_VERSION is {m.group(1)}")

    print("== Screenshots")
    shots = listing.get("screenshots") or {}
    for s in shots.get("sets") or []:
        dtype = s["displayType"]
        if dtype not in SIZES:
            errors.append(f"{dtype}: no size table in this script")
            continue
        if not 1 <= len(s["files"]) <= 10:
            errors.append(f"{dtype}: 1 to 10 screenshots per set (Apple), got {len(s['files'])}")
        for rel in s["files"]:
            try:
                name, data, size, note = load_screenshot(os.path.join(ROOT, rel), SIZES[dtype])
                print(f"  {dtype:22} {name:28} {size[0]}x{size[1]}  {len(data)} bytes" + (f"  ({note})" if note else ""))
            except (ValueError, OSError) as e:
                errors.append(f"{dtype}: {e}")
    watch = shots.get("watch")
    if watch:
        ok, msg, _ = watch_plan(watch, watch_dir)
        print(f"  Apple Watch: {msg}")
        if not ok:
            warnings.append(f"Apple Watch screenshots: {msg}")
    return errors, warnings


def watch_plan(watch, watch_dir):
    """(ok, message, {locale: (display type, [(name, data)])})."""
    need = "; ".join(f"{loc}: {', '.join(files)}" for loc, files in watch["files"].items())
    sizes = ", ".join(f"{w}x{h}" for w, h in WATCH_TYPES)
    what = (f"needed: {need} from the CI artifact {watch.get('artifact', 'ios-screenshots')} of a successful "
            f"run, each one of {sizes}, the same size in every language")
    if not watch_dir:
        return False, f"no --watch-dir given; {what}", {}
    if not os.path.isdir(watch_dir):
        return False, f"{watch_dir} does not exist (artifact not downloaded?); {what}", {}
    plan, found_sizes, problems = {}, set(), []
    for locale, files in watch["files"].items():
        items = []
        for name in files:
            try:
                fname, data, size, note = load_screenshot(os.path.join(watch_dir, name))
            except (ValueError, OSError) as e:
                problems.append(str(e))
                continue
            if size not in WATCH_TYPES:
                problems.append(f"{name} is {size[0]}x{size[1]}, not an Apple Watch size")
                continue
            found_sizes.add(size)
            items.append((fname, data, size, note))
        plan[locale] = items
    if problems:
        return False, "; ".join(problems) + f"; {what}", {}
    if len(found_sizes) != 1:
        return False, ("sizes differ: " + ", ".join(f"{w}x{h}" for w, h in sorted(found_sizes))
                       + f"; {what}"), {}
    size = found_sizes.pop()
    dtype = WATCH_TYPES[size]
    out = {loc: (dtype, [(n, d) for n, d, _, _ in items]) for loc, items in plan.items()}
    notes = {n for items in plan.values() for _, _, _, n in items if n}
    return True, (f"{sum(len(v) for v in plan.values())} files, {size[0]}x{size[1]} -> {dtype}"
                  + (f" ({', '.join(sorted(notes))})" if notes else "")), out


# ---------------------------------------------------------------------------------------------------
# Steps


class Ctx:
    def __init__(self, api, report, listing, args):
        self.api = api
        self.report = report
        self.listing = listing
        self.args = args
        self.app_id = listing["app"]["id"]
        self.bundle_id = listing["app"]["bundleId"]
        self.version_string = args.version
        self.build_number = args.build
        self.price = decimal.Decimal(args.price_jpy)
        self.app = None
        self.version = None
        self.first_version = True
        self.app_info = None
        self.app_info_included = []
        self.version_locs = {}


def step_app(c):
    body = c.api.get_ok(f"/v1/apps/{c.app_id}", what="the app")
    c.app = body["data"]
    bundle = c.app["attributes"].get("bundleId")
    if bundle != c.bundle_id:
        raise Fatal(f"App {c.app_id} has bundle id {bundle}, expected {c.bundle_id}")
    c.report.ok("App", f"{c.app['attributes'].get('name')} ({c.app_id}, {bundle})")


def vstate(v):
    a = v["attributes"]
    return a.get("appVersionState") or a.get("appStoreState") or "?"


def step_version(c):
    versions, _ = c.api.get_all(f"/v1/apps/{c.app_id}/appStoreVersions",
                                {"filter[platform]": "IOS", "limit": "200"}, "App Store versions")
    for v in versions:
        print(f"  version {v['attributes'].get('versionString')}: {vstate(v)}")
    c.first_version = not any(
        v["attributes"].get("versionString") != c.version_string
        and (v["attributes"].get("appVersionState") in ON_STORE or v["attributes"].get("appStoreState") in ON_STORE)
        for v in versions)
    target = next((v for v in versions if v["attributes"].get("versionString") == c.version_string), None)
    what = ""
    if target is not None:
        if vstate(target) not in EDITABLE_VERSION:
            raise Fatal(f"Version {c.version_string} is {vstate(target)}: it can no longer be edited "
                        "(already submitted, in review or released). Nothing was changed.")
        what = f"found ({vstate(target)})"
    else:
        editable = [v for v in versions if vstate(v) in EDITABLE_VERSION]
        if editable:
            target = editable[0]
            old = target["attributes"].get("versionString")
            ok, status, body, err = c.api.write("PATCH", f"/v1/appStoreVersions/{target['id']}", {
                "data": {"type": "appStoreVersions", "id": target["id"],
                         "attributes": {"versionString": c.version_string}}})
            if not ok:
                raise Fatal(f"Renaming version {old} to {c.version_string} failed: HTTP {status} {err}")
            what = f"renamed the editable version {old} to {c.version_string}"
        else:
            ok, status, body, err = c.api.write("POST", "/v1/appStoreVersions", {
                "data": {"type": "appStoreVersions",
                         "attributes": {"platform": "IOS", "versionString": c.version_string},
                         "relationships": {"app": {"data": {"type": "apps", "id": c.app_id}}}}})
            if not ok:
                raise Fatal(f"Creating version {c.version_string} failed: HTTP {status} {err}")
            target = body["data"]
            what = "created"
    c.version = target
    c.report.ok(f"App Store version {c.version_string}", what + (
        "; first App Store version: What's New left out" if c.first_version
        else "; the app has been on the App Store: What's New is sent"))
    want = {"copyright": c.listing["version"]["copyright"],
            "releaseType": c.listing["version"].get("releaseType", "MANUAL")}
    have = target.get("attributes") or {}
    change = {k: v for k, v in want.items() if have.get(k) != v}
    if change:
        ok, status, _, err = c.api.write("PATCH", f"/v1/appStoreVersions/{target['id']}", {
            "data": {"type": "appStoreVersions", "id": target["id"], "attributes": change}})
        if not ok:
            c.report.failed("Copyright and release type", f"HTTP {status} {err}")
        else:
            c.report.ok("Copyright and release type",
                        f"copyright '{want['copyright']}', release {want['releaseType']} (you press Release)")
    else:
        c.report.ok("Copyright and release type", "already set")


def same(a, b):
    return (a or "").replace("\r\n", "\n").strip() == (b or "").replace("\r\n", "\n").strip()


def upsert_locs(c, label, list_path, kind, parent_rel, parent_type, parent_id, fields, drop_on_error=()):
    """Create or update one localization resource per locale. Returns {locale: resource}."""
    items, _ = c.api.get_all(list_path, {"limit": "200"}, label)
    have = {i["attributes"]["locale"]: i for i in items}
    out = {}
    for locale, values in fields.items():
        existing = have.get(locale)
        if existing:
            change = {k: v for k, v in values.items() if not same(existing["attributes"].get(k), v)}
            if not change:
                c.report.ok(f"{label} ({locale})", "already up to date")
                out[locale] = existing
                continue
            body = {"data": {"type": kind, "id": existing["id"], "attributes": change}}
            method, path = "PATCH", f"/v1/{kind}/{existing['id']}"
        else:
            change = dict(values)
            body = {"data": {"type": kind, "attributes": dict(change, locale=locale),
                             "relationships": {parent_rel: {"data": {"type": parent_type, "id": parent_id}}}}}
            method, path = "POST", f"/v1/{kind}"
        ok, status, resp, err = c.api.write(method, path, body)
        dropped = []
        if not ok:
            retry = [f for f in drop_on_error if f in change and f in err]
            if retry:
                for f in retry:
                    body["data"]["attributes"].pop(f, None)
                dropped = retry
                ok, status, resp, err = c.api.write(method, path, body)
        if ok:
            res = (resp or {}).get("data") or existing or {}
            out[locale] = res
            note = ("created" if method == "POST" else "updated") + ": " + ", ".join(sorted(change))
            if dropped:
                note += f" (Apple refused {', '.join(dropped)} for this version, left out)"
            c.report.ok(f"{label} ({locale})", note)
        else:
            c.report.failed(f"{label} ({locale})", f"HTTP {status} {err}")
    extra = sorted(set(have) - set(fields))
    if extra:
        print(f"  note: {label} also has {', '.join(extra)} (left as they are)")
    return out


def step_app_info(c):
    infos, included = c.api.get_all(f"/v1/apps/{c.app_id}/appInfos",
                                    {"include": "primaryCategory,secondaryCategory", "limit": "20"}, "app infos")
    def st(i):
        a = i["attributes"]
        return a.get("state") or a.get("appStoreState") or "?"
    editable = [i for i in infos if st(i) in EDITABLE_APP_INFO]
    if not editable:
        raise Fatal("No editable app information (states: " + ", ".join(st(i) for i in infos) + ")")
    info = editable[0]
    c.app_info = info
    fields = {loc: {k: v[k] for k in APP_INFO_FIELDS if v.get(k)} for loc, v in c.listing["localizations"].items()}
    upsert_locs(c, "App information", f"/v1/appInfos/{info['id']}/appInfoLocalizations", "appInfoLocalizations",
                "appInfo", "appInfos", info["id"], fields)

    # Categories (app level).
    want = c.listing["categories"]
    rel = info.get("relationships") or {}
    cur = {k: ((rel.get(f"{k}Category") or {}).get("data") or {}).get("id") for k in ("primary", "secondary")}
    if cur == {"primary": want["primary"], "secondary": want.get("secondary")}:
        c.report.ok("Categories", f"already {want['primary']} / {want.get('secondary')}")
    else:
        rels = {"primaryCategory": {"data": {"type": "appCategories", "id": want["primary"]}}}
        if want.get("secondary"):
            rels["secondaryCategory"] = {"data": {"type": "appCategories", "id": want["secondary"]}}
        ok, status, _, err = c.api.write("PATCH", f"/v1/appInfos/{info['id']}", {
            "data": {"type": "appInfos", "id": info["id"], "relationships": rels}})
        if ok:
            c.report.ok("Categories", f"{want['primary']} / {want.get('secondary')}")
        else:
            c.report.failed("Categories", f"HTTP {status} {err}")

    # Age rating declaration: only the attributes that differ (Apple: include only what you modify).
    answers = {k: v for k, v in c.listing["ageRating"].items() if not k.startswith("_")}
    status, body, raw = c.api.get(f"/v1/appInfos/{info['id']}/ageRatingDeclaration")
    decl = (body or {}).get("data") if status == 200 else None
    if not decl:
        c.report.failed("Age rating", f"could not read the declaration: HTTP {status} {error_summary(raw)}")
        return
    cur = decl.get("attributes") or {}
    change = {k: v for k, v in answers.items() if cur.get(k) != v}
    if not change:
        c.report.ok("Age rating", "answers already set")
    else:
        ok, status, _, err = c.api.write("PATCH", f"/v1/ageRatingDeclarations/{decl['id']}", {
            "data": {"type": "ageRatingDeclarations", "id": decl["id"], "attributes": change}})
        if not ok:
            c.report.failed("Age rating", f"HTTP {status} {err}")
            return
        c.report.ok("Age rating", f"{len(change)} answer(s) set")
    if not c.api.dry:
        status, body, _ = c.api.get(f"/v1/appInfos/{info['id']}")
        rating = (((body or {}).get("data") or {}).get("attributes") or {}).get("appStoreAgeRating")
        print(f"  App Store age rating now: {rating or '(not computed yet)'} (expected FOUR_PLUS, 4+)")
        if rating and rating != "FOUR_PLUS":
            c.report.todo(f"Age rating shows {rating}, not 4+: check the answers in App Information > Age Rating "
                          f"({ASC}/apps/{c.app_id}/distribution/info).")


def step_version_locs(c):
    fields = {}
    for loc, v in c.listing["localizations"].items():
        f = {k: v[k] for k in VERSION_FIELDS if v.get(k)}
        if c.first_version:
            f.pop("whatsNew", None)
        fields[loc] = f
    c.version_locs = upsert_locs(
        c, "Version information", f"/v1/appStoreVersions/{c.version['id']}/appStoreVersionLocalizations",
        "appStoreVersionLocalizations", "appStoreVersion", "appStoreVersions", c.version["id"], fields,
        drop_on_error=("whatsNew",))


def step_app_level(c):
    a = c.app["attributes"]
    change = {}
    if c.listing.get("primaryLocale") and a.get("primaryLocale") != c.listing["primaryLocale"]:
        change["primaryLocale"] = c.listing["primaryLocale"]
    if c.listing.get("contentRightsDeclaration") and a.get("contentRightsDeclaration") != c.listing["contentRightsDeclaration"]:
        change["contentRightsDeclaration"] = c.listing["contentRightsDeclaration"]
    if not change:
        c.report.ok("Primary language and content rights",
                    f"already {a.get('primaryLocale')} / {a.get('contentRightsDeclaration')}")
        return
    ok, status, _, err = c.api.write("PATCH", f"/v1/apps/{c.app_id}", {
        "data": {"type": "apps", "id": c.app_id, "attributes": change}})
    if ok:
        c.report.ok("Primary language and content rights",
                    ", ".join(f"{k} {a.get(k)} -> {v}" for k, v in change.items()))
    else:
        c.report.failed("Primary language and content rights", f"HTTP {status} {err}")


AGREEMENT = re.compile(r"agreement|contract|paid app", re.I)


def step_price(c):
    step = f"Price {c.price:,} JPY (base territory JPN)"
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    status, body, raw = c.api.get(f"/v1/apps/{c.app_id}/appPriceSchedule", {"include": "baseTerritory"})
    if status == 200 and (body or {}).get("data"):
        sched = body["data"]
        base = (((sched.get("relationships") or {}).get("baseTerritory") or {}).get("data") or {}).get("id")
        # An app that never had a price has a schedule record but no manual prices: Apple answers 404 here
        # ("no resource of type 'null'"), which means "none yet", not an error.
        m_status, m_body, m_raw = c.api.get(f"/v1/appPriceSchedules/{sched['id']}/manualPrices",
                                            {"include": "appPricePoint,territory", "filter[territory]": "JPN",
                                             "limit": "200"})
        if m_status == 200:
            prices, included = list(m_body.get("data") or []), list(m_body.get("included") or [])
        else:
            prices, included = [], []
            if m_status != 404:
                print(f"  reading the manual prices: HTTP {m_status} {error_summary(m_raw)} (setting it anyway)")
        points = {i["id"]: i for i in included if i.get("type") == "appPricePoints"}
        current, later = [], []
        for p in prices:
            a = p.get("attributes") or {}
            pp = ((p.get("relationships") or {}).get("appPricePoint") or {}).get("data") or {}
            amount = ((points.get(pp.get("id")) or {}).get("attributes") or {}).get("customerPrice")
            if (a.get("startDate") or "") <= today and (not a.get("endDate") or a["endDate"] > today):
                current.append(amount)
            elif (a.get("startDate") or "") > today:
                later.append(amount)
        print(f"  current schedule: base {base}, JPN now {current or 'none'}, scheduled {later or 'none'}")
        if base == "JPN" and len(current) == 1 and current[0] is not None \
                and decimal.Decimal(current[0]) == c.price and not later:
            c.report.ok(step, "already set")
            return
    elif status not in (200, 404):
        print(f"  reading the price schedule: HTTP {status} {error_summary(raw)} (setting it anyway)")

    points, _ = c.api.get_all(f"/v1/apps/{c.app_id}/appPricePoints",
                              {"filter[territory]": "JPN", "limit": "200"}, "JPN price points")
    match = [p for p in points if decimal.Decimal(p["attributes"]["customerPrice"]) == c.price]
    if not match:
        near = sorted(points, key=lambda p: abs(decimal.Decimal(p["attributes"]["customerPrice"]) - c.price))[:4]
        c.report.failed(step, f"App Store Connect has no JPN price point of {c.price} yen "
                              f"({len(points)} points listed; nearest: "
                              + ", ".join(p["attributes"]["customerPrice"] for p in near) + ")")
        return
    point = match[0]
    ok, status, _, err = c.api.write("POST", "/v1/appPriceSchedules", {
        "data": {"type": "appPriceSchedules", "attributes": {}, "relationships": {
            "app": {"data": {"type": "apps", "id": c.app_id}},
            "baseTerritory": {"data": {"type": "territories", "id": "JPN"}},
            "manualPrices": {"data": [{"type": "appPrices", "id": "${price-jpn}"}]}}},
        "included": [{"type": "appPrices", "id": "${price-jpn}",
                      "attributes": {"startDate": None, "endDate": None},
                      "relationships": {"appPricePoint": {"data": {"type": "appPricePoints", "id": point["id"]}}}}]})
    if ok:
        c.report.ok(step, f"price point {point['attributes']['customerPrice']} JPY; other territories "
                          "follow automatically (Apple's equalization)")
    elif AGREEMENT.search(err):
        c.report.skipped(step, "Apple refused it because the Paid Apps Agreement is not active yet: "
                               f"HTTP {status} {err}")
        c.report.todo(f"Price: once the Paid Apps Agreement is active, run appstore-prepare again, or set it by "
                      f"hand: {ASC}/apps/{c.app_id}/distribution/pricing > Price Schedule > \"Add Pricing\" > "
                      f"Japan > ¥{c.price:,} > \"Next\" > \"Next\" > \"Confirm\".")
    else:
        c.report.failed(step, f"HTTP {status} {err}")


def step_availability(c):
    """Every territory except listing.json availability.excludeTerritories (the 27 EU member states).
    An existing setup is changed only where it differs from that: excluded territories are switched off with
    PATCH /v1/territoryAvailabilities/{id}; other territories that are off are reported, not switched on."""
    step = "Availability"
    cfg = c.listing.get("availability") or {}
    exclude = set(cfg.get("excludeTerritories") or [])
    new_territories = bool(cfg.get("availableInNewTerritories", True))
    territories, _ = c.api.get_all("/v1/territories", {"limit": "200"}, "territories")
    known = {t["id"] for t in territories}
    unknown = sorted(exclude - known)
    if unknown:
        print(f"  not App Store territories (ignored): {', '.join(unknown)}")
    exclude &= known
    summary = (f"{len(known) - len(exclude)} of {len(known)} territories; not in {len(exclude)} "
               f"({', '.join(sorted(exclude)) or 'none'}); new territories: {new_territories}")

    status, body, raw = c.api.get(f"/v1/apps/{c.app_id}/appAvailabilityV2")
    if status == 200 and (body or {}).get("data"):
        avail_id = body["data"]["id"]
        tas, included = c.api.get_all(f"/v2/appAvailabilities/{avail_id}/territoryAvailabilities",
                                      {"include": "territory", "limit": "200"}, "territory availabilities")
        changed, failed, off_elsewhere = [], [], []
        for ta in tas:
            terr = (((ta.get("relationships") or {}).get("territory") or {}).get("data") or {}).get("id")
            available = bool((ta.get("attributes") or {}).get("available"))
            if terr in exclude and available:
                ok, st, _, err = c.api.write("PATCH", f"/v1/territoryAvailabilities/{ta['id']}", {
                    "data": {"type": "territoryAvailabilities", "id": ta["id"], "attributes": {"available": False}}})
                (changed if ok else failed).append(terr if ok else f"{terr} (HTTP {st} {err})")
            elif terr and terr not in exclude and not available:
                off_elsewhere.append(terr)
        if off_elsewhere:
            print(f"  also off, left as they are: {', '.join(sorted(off_elsewhere))}")
        if failed:
            c.report.failed(step, "switching off failed for " + "; ".join(failed))
        elif changed:
            c.report.ok(step, f"switched off {len(changed)} territories ({', '.join(sorted(changed))}); {summary}")
        else:
            c.report.ok(step, f"already as wanted (not changed): {summary}")
        return
    if status not in (200, 404):
        c.report.failed(step, f"reading it failed: HTTP {status} {error_summary(raw)}")
        return
    refs, included = [], []
    for t in territories:
        local = "${ta-" + t["id"] + "}"
        refs.append({"type": "territoryAvailabilities", "id": local})
        included.append({"type": "territoryAvailabilities", "id": local,
                         "attributes": {"available": t["id"] not in exclude},
                         "relationships": {"territory": {"data": {"type": "territories", "id": t["id"]}}}})
    ok, status, _, err = c.api.write("POST", "/v2/appAvailabilities", {
        "data": {"type": "appAvailabilities", "attributes": {"availableInNewTerritories": new_territories},
                 "relationships": {"app": {"data": {"type": "apps", "id": c.app_id}},
                                   "territoryAvailabilities": {"data": refs}}},
        "included": included})
    if ok:
        c.report.ok(step, summary)
    else:
        c.report.failed(step, f"HTTP {status} {err}")


def step_review(c):
    review = c.listing["review"]
    status, body, raw = c.api.get(f"/v1/appStoreVersions/{c.version['id']}/appStoreReviewDetail")
    detail = (body or {}).get("data") if status == 200 else None
    if status not in (200, 404):
        c.report.failed("App Review information", f"reading it failed: HTTP {status} {error_summary(raw)}")
        return
    cur = (detail or {}).get("attributes") or {}
    status, beta_body, _ = c.api.get(f"/v1/apps/{c.app_id}/betaAppReviewDetail")
    beta = (((beta_body or {}).get("data") or {}).get("attributes") or {}) if status == 200 else {}
    for f, _ in CONTACT_FIELDS:
        REDACT.add(str(cur.get(f) or ""))
        REDACT.add(str(beta.get(f) or ""))
    base = {"notes": review["notes"], "demoAccountRequired": bool(review.get("demoAccountRequired", False))}
    contact, copied, missing = {}, [], []
    for f, label in CONTACT_FIELDS:
        if cur.get(f):
            continue
        if beta.get(f):
            contact[f] = beta[f]
            copied.append(label)
        else:
            missing.append(label)

    def send(attrs):
        """(ok, status, error, attributes sent)."""
        if detail:
            change = {k: v for k, v in attrs.items() if cur.get(k) != v}
            if not change:
                return True, 200, "", {}
            ok, st, _, err = c.api.write("PATCH", f"/v1/appStoreReviewDetails/{detail['id']}", {
                "data": {"type": "appStoreReviewDetails", "id": detail["id"], "attributes": change}})
            return ok, st, err, change
        ok, st, _, err = c.api.write("POST", "/v1/appStoreReviewDetails", {
            "data": {"type": "appStoreReviewDetails", "attributes": attrs,
                     "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions",
                                                                    "id": c.version["id"]}}}}})
        return ok, st, err, attrs

    ok, status, err, sent = send(dict(base, **contact))
    if not ok and contact:
        print(f"  Apple refused the copied contact fields (HTTP {status} {err}); trying the notes alone")
        missing += copied
        copied = []
        ok, status, err, sent = send(base)
    if not ok:
        c.report.failed("App Review notes and sign-in", f"HTTP {status} {err}")
        return
    c.report.ok("App Review notes and sign-in",
                "notes from listing.json, sign-in not required" if sent else "already set")
    if copied:
        c.report.ok("App Review contact", "copied from TestFlight Beta App Review Information into empty fields: "
                    + ", ".join(copied) + " (values not printed)")
    elif not missing:
        c.report.ok("App Review contact", "already filled in")
    if missing:
        c.report.skipped("App Review contact", "empty in App Store and in TestFlight: " + ", ".join(missing))
        c.report.todo(f"App Review Information > Contact Information: fill in {', '.join(missing)} on the version "
                      f"page ({ASC}/apps/{c.app_id}/distribution/ios/version/inflight). The phone number needs "
                      "the + and country code (for example +81 90 1234 5678).")


def upload_screenshots(c, loc_name, loc_id, dtype, files, other_types_to_clear=()):
    """Make the set of `dtype` in this localization hold exactly `files` [(name, bytes)], in order."""
    step = f"Screenshots {dtype} ({loc_name})"
    sets, _ = c.api.get_all(f"/v1/appStoreVersionLocalizations/{loc_id}/appScreenshotSets",
                            {"limit": "50"}, f"screenshot sets ({loc_name})")
    by_type = {s["attributes"]["screenshotDisplayType"]: s for s in sets}
    for other in other_types_to_clear:
        s = by_type.get(other)
        if s and other != dtype:
            ok, status, _, err = c.api.write("DELETE", f"/v1/appScreenshotSets/{s['id']}")
            print(f"  removed the {other} set ({loc_name}): only one Apple Watch size is allowed" if ok
                  else f"  could not remove the {other} set ({loc_name}): HTTP {status} {err}")
    want = [(name, data, hashlib.md5(data).hexdigest()) for name, data in files]
    s = by_type.get(dtype)
    if s:
        shots, _ = c.api.get_all(f"/v1/appScreenshotSets/{s['id']}/appScreenshots", {"limit": "50"}, step)
        have = [((x["attributes"].get("fileName"), x["attributes"].get("sourceFileChecksum"),
                  ((x["attributes"].get("assetDeliveryState") or {}).get("state")))) for x in shots]
        if have == [(n, m, "COMPLETE") for n, _, m in want]:
            c.report.ok(step, f"already the same {len(want)} screenshot(s)")
            return
        for x in shots:
            ok, status, _, err = c.api.write("DELETE", f"/v1/appScreenshots/{x['id']}")
            if not ok:
                c.report.failed(step, f"deleting an old screenshot failed: HTTP {status} {err}")
                return
        if shots:
            print(f"  deleted {len(shots)} old screenshot(s)")
        set_id = s["id"]
    else:
        ok, status, body, err = c.api.write("POST", "/v1/appScreenshotSets", {
            "data": {"type": "appScreenshotSets", "attributes": {"screenshotDisplayType": dtype},
                     "relationships": {"appStoreVersionLocalization": {
                         "data": {"type": "appStoreVersionLocalizations", "id": loc_id}}}}})
        if not ok:
            c.report.failed(step, f"creating the set failed: HTTP {status} {err}")
            return
        set_id = body["data"]["id"]

    ids = []
    for name, data, md5 in want:
        # 1. Reserve.
        ok, status, body, err = c.api.write("POST", "/v1/appScreenshots", {
            "data": {"type": "appScreenshots", "attributes": {"fileName": name, "fileSize": len(data)},
                     "relationships": {"appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}}}})
        if not ok:
            c.report.failed(step, f"{name}: reservation failed: HTTP {status} {err}")
            return
        shot = body["data"]
        # 2. Upload each part as the reservation says.
        for op in shot["attributes"].get("uploadOperations") or []:
            part = data[op["offset"]:op["offset"] + op["length"]]
            ok, why = c.api.upload_part(op, part)
            if not ok:
                c.report.failed(step, f"{name}: upload failed: {why}")
                return
        # 3. Commit with the MD5 of the whole file.
        ok, status, _, err = c.api.write("PATCH", f"/v1/appScreenshots/{shot['id']}", {
            "data": {"type": "appScreenshots", "id": shot["id"],
                     "attributes": {"uploaded": True, "sourceFileChecksum": md5}}})
        if not ok:
            c.report.failed(step, f"{name}: commit failed: HTTP {status} {err}")
            return
        ids.append((shot["id"], name))

    # 4. Wait until App Store Connect has processed them.
    pending = dict(ids)
    failures = []
    deadline = time.time() + 600
    while pending and time.time() < deadline:
        for sid in list(pending):
            status, body, _ = c.api.get(f"/v1/appScreenshots/{sid}")
            st = ((((body or {}).get("data") or {}).get("attributes") or {}).get("assetDeliveryState") or {})
            if st.get("state") == "COMPLETE":
                pending.pop(sid)
            elif st.get("state") == "FAILED":
                errs = "; ".join(f"{e.get('code')}: {e.get('description')}" for e in st.get("errors") or [])
                failures.append(f"{pending.pop(sid)}: {errs or 'FAILED'}")
        if pending:
            c.api.sleep(5)
    if failures:
        c.report.failed(step, "Apple could not process " + "; ".join(failures))
        return
    if pending:
        c.report.failed(step, "still processing after 10 minutes: " + ", ".join(pending.values())
                        + " (run this again later)")
        return
    ok, status, _, err = c.api.write("PATCH", f"/v1/appScreenshotSets/{set_id}/relationships/appScreenshots", {
        "data": [{"type": "appScreenshots", "id": sid} for sid, _ in ids]})
    if not ok:
        print(f"  setting the order failed (HTTP {status} {err}); they are in upload order")
    c.report.ok(step, f"{len(ids)} uploaded: " + ", ".join(n for _, n in ids))


def step_screenshots(c):
    shots = c.listing["screenshots"]
    loaded = []
    for s in shots["sets"]:
        try:
            files = [load_screenshot(os.path.join(ROOT, rel), SIZES[s["displayType"]]) for rel in s["files"]]
        except (ValueError, OSError) as e:
            c.report.failed(f"Screenshots {s['displayType']}", str(e))
            continue
        loaded.append((s["displayType"], [(n, d) for n, d, _, _ in files]))
    for locale in c.listing["localizations"]:
        loc = c.version_locs.get(locale)
        if not loc or not loc.get("id"):
            c.report.failed(f"Screenshots ({locale})", "no version localization to put them in")
            continue
        for dtype, files in loaded:
            upload_screenshots(c, locale, loc["id"], dtype, files)

    watch = shots.get("watch")
    if not watch:
        return
    ok, msg, plan = watch_plan(watch, c.args.watch_dir)
    if not ok:
        c.report.skipped("Screenshots Apple Watch", msg)
        c.report.todo("Apple Watch screenshots (required for an app with a watch app): " + msg + ". Re-run CI on "
                      "this branch (job 'ios'), then appstore-prepare; or upload them by hand on the version page "
                      f"({ASC}/apps/{c.app_id}/distribution/ios/version/inflight) under Apple Watch.")
        return
    print(f"  Apple Watch: {msg}")
    for locale in c.listing["localizations"]:
        loc = c.version_locs.get(locale)
        if not loc or locale not in plan:
            c.report.failed(f"Screenshots Apple Watch ({locale})", "no localization or no files for it")
            continue
        dtype, files = plan[locale]
        upload_screenshots(c, locale, loc["id"], dtype, files, other_types_to_clear=ALL_WATCH_TYPES)


def step_build(c):
    step = f"Build {c.version_string} ({c.build_number})"
    build = None
    tries = max(1, int(c.args.build_wait_minutes * 2))
    for attempt in range(tries):
        status, body, raw = c.api.get("/v1/builds", {
            "filter[app]": c.app_id, "filter[version]": c.build_number,
            "filter[preReleaseVersion.version]": c.version_string, "filter[preReleaseVersion.platform]": "IOS",
            "limit": "10"})
        if status != 200:
            c.report.failed(step, f"listing builds failed: HTTP {status} {error_summary(raw)}")
            return
        builds = body.get("data") or []
        if builds:
            build = builds[0]
            state = build["attributes"].get("processingState")
            print(f"  build {c.version_string} ({c.build_number}): {state}")
            if state == "VALID":
                break
            if state in ("FAILED", "INVALID"):
                c.report.failed(step, f"Apple could not process it: {state}")
                return
        else:
            print(f"  build {c.version_string} ({c.build_number}) not listed yet")
        if attempt < tries - 1:
            c.api.sleep(30)
    else:
        c.report.failed(step, f"not processed (VALID) within {c.args.build_wait_minutes:g} minutes; "
                              "run this again once TestFlight shows it")
        return
    a = build["attributes"]
    if a.get("buildAudienceType") == "INTERNAL_ONLY":
        c.report.failed(step, "it was uploaded for internal TestFlight testing only (buildAudienceType "
                              "INTERNAL_ONLY) and cannot be submitted to the App Store")
        return
    if a.get("usesNonExemptEncryption") is None:
        c.report.todo("Export compliance: the build shows no encryption answer. On the version page, answer the "
                      "encryption questions (Apple's operating system encryption only; see compliance.md).")
    status, body, _ = c.api.get(f"/v1/appStoreVersions/{c.version['id']}/build")
    current = ((body or {}).get("data") or {}).get("id") if status == 200 else None
    if current == build["id"]:
        c.report.ok(step, "already attached to the version")
        return
    ok, status, _, err = c.api.write("PATCH", f"/v1/appStoreVersions/{c.version['id']}/relationships/build", {
        "data": {"type": "builds", "id": build["id"]}})
    if ok:
        c.report.ok(step, "attached to the version (usesNonExemptEncryption="
                          f"{a.get('usesNonExemptEncryption')})")
    else:
        c.report.failed(step, f"HTTP {status} {err}")



SUBMISSION_OPEN = ("READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW", "UNRESOLVED_ISSUES")


def step_submit(c):
    """Submit the version for App Review (only with --submit, and only after every other step was OK).
    Apple: POST /v1/reviewSubmissions, POST /v1/reviewSubmissionItems, PATCH /v1/reviewSubmissions/{id}
    with submitted=true. If Apple refuses (for example App Privacy not published, or an agreement missing),
    its answer is printed and the draft submission stays in App Store Connect ("Add for Review" done);
    running this again reuses it. Release stays MANUAL: an approved version waits for "Release"."""
    step = "Submit for App Review"
    subs, _ = c.api.get_all("/v1/reviewSubmissions", {
        "filter[app]": c.app_id, "filter[platform]": "IOS", "filter[state]": ",".join(SUBMISSION_OPEN),
        "include": "items", "limit": "50"}, "review submissions")
    sub = None
    for s_ in subs:
        state = (s_.get("attributes") or {}).get("state")
        if state in ("WAITING_FOR_REVIEW", "IN_REVIEW"):
            c.report.ok(step, f"already submitted ({state}); nothing changed")
            return
        if state == "UNRESOLVED_ISSUES":
            c.report.failed(step, "App Review sent back an earlier submission (Unresolved Issues). Read the "
                                  f"message in App Store Connect first: {ASC}/apps/{c.app_id}/distribution/reviewsubmissions")
            return
        if state == "READY_FOR_REVIEW" and sub is None:
            sub = s_
    if sub is None:
        ok, status, body, err = c.api.write("POST", "/v1/reviewSubmissions", {
            "data": {"type": "reviewSubmissions", "attributes": {"platform": "IOS"},
                     "relationships": {"app": {"data": {"type": "apps", "id": c.app_id}}}}})
        if not ok:
            c.report.failed(step, f"creating the submission: HTTP {status} {err}")
            return
        sub = body["data"]
        print(f"  draft submission created ({sub['id']})")
    else:
        print(f"  using the draft submission {sub['id']}")
    items, _ = c.api.get_all(f"/v1/reviewSubmissions/{sub['id']}/items",
                             {"include": "appStoreVersion", "limit": "50"}, "submission items")
    has_version = any((((i.get("relationships") or {}).get("appStoreVersion") or {}).get("data") or {}).get("id")
                      == c.version["id"] for i in items)
    if not has_version:
        ok, status, body, err = c.api.write("POST", "/v1/reviewSubmissionItems", {
            "data": {"type": "reviewSubmissionItems", "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub["id"]}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": c.version["id"]}}}}})
        if not ok:
            c.report.failed(step, f"adding version {c.version_string} to the submission (Apple's answer, "
                                  f"usually what is still missing): HTTP {status} {err}")
            return
        print(f"  version {c.version_string} added to the submission")
    ok, status, body, err = c.api.write("PATCH", f"/v1/reviewSubmissions/{sub['id']}", {
        "data": {"type": "reviewSubmissions", "id": sub["id"], "attributes": {"submitted": True}}})
    if not ok:
        c.report.failed(step, f"Apple did not accept the submission (usually what is still missing): "
                              f"HTTP {status} {err}. The draft stays in App Store Connect; run this again "
                              "once it is fixed.")
        return
    state = ((body or {}).get("data") or {}).get("attributes", {}).get("state", "?")
    c.report.ok(step, f"submitted: {state}. Release is MANUAL: once approved, it waits for \"Release\".")


def checklist(c):
    app = f"{ASC}/apps/{c.app_id}"
    items = [
        f"App Privacy ({app}/distribution/privacy): \"Get Started\" > \"No, we do not collect data from this app\" > "
        "\"Save\" > \"Publish\" (and \"Publish\" again in the dialog). The Product Page Preview then shows "
        "\"Data Not Collected\".",
        f"Paid Apps Agreement, tax and banking ({ASC}/business, Account Holder): Agreements tab > Paid Apps > "
        "\"View and Agree to Terms\" > \"Agree\"; Tax Forms > \"Add Tax Info\" > \"Submit\"; Bank Accounts > "
        "\"Add Bank Account\" > ... > \"Add\". Without an active Paid Apps Agreement Apple only allows a free app.",
        f"EU Digital Services Act trader status ({ASC}/business): Agreements tab > Compliance > Digital Services Act "
        "> \"Complete Compliance Requirements\" > \"This is not a trader account\" > \"Done\". The app is not on the "
        "App Store in the 27 EU countries (Apple: distributing 'on the App Store only outside the EU' is not acting "
        "as a trader there); Apple still asks everyone to declare it.",
        f"Digital Goods and Services, only if App Information shows it ({app}/distribution/info): \"Get Started\". "
        "TwinKeys Remote sells nothing inside the app (no In-App Purchase). The EU answers are locked for 12 months.",
        f"Availability, only to exclude more territories ({app}/distribution/pricing): App Availability > "
        "\"Manage\" > \"Manage Availability\" > untick > \"Next\" > confirm (the 27 EU countries are already off). "
        "China mainland may need an ICP filing number.",
    ]
    items += c.report.manual
    if c.args.submit:
        items.append(f"Submitting: done by this run (--submit) when every step was OK; see \"Submit for App Review\" "
                     f"above. Status: {app}/distribution/reviewsubmissions. Once approved, press \"Release\".")
    else:
        items.append(f"Finally, on the version page ({app}/distribution/ios/version/inflight): check everything, then "
                     "\"Add for Review\" and, in the draft submission, \"Submit for Review\". Without --submit this "
                     "script does neither.")
    return items


# ---------------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listing", default=DEFAULT_LISTING)
    ap.add_argument("--version", help="App Store version (default: listing.json)")
    ap.add_argument("--build", help="build number (default: listing.json)")
    ap.add_argument("--price-jpy", help="price in yen (default: listing.json)")
    ap.add_argument("--watch-dir", help="folder with the Apple Watch PNGs from the CI artifact")
    ap.add_argument("--build-wait-minutes", type=float, default=30.0)
    ap.add_argument("--check", action="store_true", help="only check listing.json and the screenshots (offline)")
    ap.add_argument("--dry-run", action="store_true", help="print the planned requests against a made-up new app")
    ap.add_argument("--submit", action="store_true",
                    help="after every step is OK, submit the version for App Review (release stays manual)")
    args = ap.parse_args(argv)

    with open(args.listing, encoding="utf-8") as f:
        listing = json.load(f)
    args.version = (args.version or listing["version"]["versionString"]).strip()
    args.build = (args.build or listing["version"]["build"]).strip()
    args.price_jpy = str(args.price_jpy or listing["price"]["customerPrice"]).strip()
    for name, value, pattern in (("version", args.version, r"\d+(\.\d+){1,2}"), ("build", args.build, r"\d+(\.\d+){0,2}"),
                                 ("price", args.price_jpy, r"[1-9]\d{0,6}")):
        if not re.fullmatch(pattern, value):
            print(f"::error::Not a valid {name}: {value!r}")
            return 2

    errors, warnings = check_listing(listing, args.watch_dir)
    for w in warnings:
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")
    if errors:
        print("listing.json does not meet Apple's limits; nothing was sent.")
        return 1
    if args.check:
        print("Check OK" + (f" ({len(warnings)} warning(s))" if warnings else ""))
        return 0

    if args.dry_run:
        api = DryApi(listing["app"]["bundleId"])
        print("\n== DRY RUN: nothing is sent. App Store Connect is played by a made-up new app record "
              "(version 1.0 Prepare for Submission, Japanese only, no price, no screenshots).")
    else:
        api = Api()
    report = Report()
    c = Ctx(api, report, listing, args)
    print(f"\n== TwinKeys Remote {args.version} (build {args.build}), {int(args.price_jpy):,} JPY; "
          + ("preparing it and submitting it for App Review" if args.submit
             else "preparing for App Review without submitting"))

    steps = [("App", step_app, True), ("App Store version", step_version, True),
             ("App information", step_app_info, False), ("Version information", step_version_locs, False),
             ("Primary language and content rights", step_app_level, False), ("Price", step_price, False),
             ("Availability", step_availability, False), ("App Review information", step_review, False),
             ("Screenshots", step_screenshots, False), ("Build", step_build, False)]
    for name, fn, fatal in steps:
        print(f"\n-- {name}")
        try:
            fn(c)
        except Fatal as e:
            report.failed(name, str(e))
            if fatal:
                print("Stopping: the following steps need this one.")
                break
        except Exception as e:  # a bug or an unexpected answer: report it, go on with the next step
            report.failed(name, f"{type(e).__name__}: {redact(str(e))}")

    if args.submit:
        print("\n-- Submit for App Review")
        if report.any_failed:
            report.skipped("Submit for App Review", "not tried: a step above failed")
        else:
            try:
                step_submit(c)
            except Fatal as e:
                report.failed("Submit for App Review", str(e))
            except Exception as e:
                report.failed("Submit for App Review", f"{type(e).__name__}: {redact(str(e))}")

    print("\n== Summary")
    for status, step, detail in report.rows:
        print(f"  {status:8} {step}")
    print("\n== Still to do by hand in App Store Connect (URLs may differ slightly; the sidebar has each page)")
    for i, item in enumerate(checklist(c), 1):
        print(f"  {i}. {item}")
    if args.dry_run:
        print(f"\nDry run: {sum(api.counts.values())} write request(s) planned "
              f"({', '.join(f'{k} {v}' for k, v in sorted(api.counts.items()))}).")
    if report.any_failed:
        print("\nSome steps failed (see above). Fix them and run this again: finished steps are skipped.")
        return 1
    print("\nDone. " + ("Submitted for App Review (see above)." if args.submit else "Nothing was submitted."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
