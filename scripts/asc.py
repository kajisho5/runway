#!/usr/bin/env python3
"""runway 用 App Store Connect API ツール（Python 3 + pyjwt + cryptography、署名鍵の暗号処理は openssl）。

API のエンドポイントとフィールドは Apple の App Store Connect API（OpenAPI 仕様）に準拠：
  GET /v1/apps, GET /v1/users, GET /v1/builds,
  GET/POST/DELETE /v1/certificates,
  POST /v1/buildUploads, POST/PATCH /v1/buildUploadFiles, GET /v1/buildUploads/{id}
（アップロードと証明書の処理は kajisho5/crosspoint の .github/scripts/asc_upload.py / asc_certs.py で実績のあるもの）

使い方:
  asc.py write-key                         環境変数の .p8 を $RUNNER_TEMP/keys/AuthKey_<KEY_ID>.p8 に書き出す
  asc.py check <bundle id>                 鍵の形式・API 認証・アプリ記録・Admin 権限を確認
  asc.py next-build <bundle id> <platform> TestFlight 上の最大ビルド番号 + 1 を出力
  asc.py certs-list                        証明書一覧
  asc.py cert-create <TYPE[,TYPE...]> <csr.pem> <out.cer>   先頭から順に試す。全部ダメなら終了コード 3
  asc.py revoke-all                        API で作った DEVELOPMENT / DISTRIBUTION 証明書をすべて失効
  asc.py decrypt-key <in.enc> <out.key>    署名用秘密鍵を復号（.p8 から導くパスフレーズ候補を順に試す）
  asc.py encrypt-key <in.key> <out.enc>    署名用秘密鍵を暗号化（標準パスフレーズ）
  asc.py upload <ipa> <bundle id> <version> <build> <platform>

環境変数:
  ASC_KEY_ID, ASC_ISSUER_ID
  ASC_KEY_P8_BASE64   .p8 の中身（PEM のまま、または base64）
  SIGNING_KEY_PASSWORD（任意）署名用秘密鍵のパスフレーズ。無ければ .p8 から導出
  RUNNER_TEMP
platform: ios | tvos | visionos
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.appstoreconnect.apple.com"
PLATFORMS = {"ios": "IOS", "tvos": "TV_OS", "visionos": "VISION_OS"}
MANAGED_CERT_TYPES = {"DEVELOPMENT", "DISTRIBUTION", "IOS_DEVELOPMENT", "IOS_DISTRIBUTION"}


def die(msg, code=1):
    print(f"::error::{msg}")
    sys.exit(code)


def env(name):
    value = os.environ.get(name, "")
    if not value.strip():
        die(f"環境変数 {name} が空（Secrets 未登録の可能性）")
    return value


def p8_raw():
    return env("ASC_KEY_P8_BASE64")


def p8_pem():
    raw = p8_raw()
    if "BEGIN PRIVATE KEY" in raw:
        pem = raw
    else:
        try:
            pem = base64.b64decode("".join(raw.split())).decode()
        except Exception:
            die("ASC_KEY_P8_BASE64 が PEM でも base64 でもない")
        if "BEGIN PRIVATE KEY" not in pem:
            die("ASC_KEY_P8_BASE64 を base64 として読んだが PEM ではなかった")
    return pem.replace("\r\n", "\n").strip() + "\n"


def key_path():
    return os.path.join(env("RUNNER_TEMP"), "keys", f"AuthKey_{env('ASC_KEY_ID').strip()}.p8")


def token():
    import jwt  # pyjwt

    now = int(time.time())
    return jwt.encode(
        {"iss": env("ASC_ISSUER_ID").strip(), "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
        p8_pem(),
        algorithm="ES256",
        headers={"kid": env("ASC_KEY_ID").strip(), "typ": "JWT"},
    )


def api(method, path, body=None, tolerate=False):
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
        if tolerate:
            return e.code, text
        print(f"{method} {path.split('?')[0]} -> HTTP {e.code}")
        print(text[:4000])
        sys.exit(1)


def app_id(bundle_id):
    q = urllib.parse.urlencode({"filter[bundleId]": bundle_id, "fields[apps]": "bundleId,name"})
    _, body = api("GET", f"/v1/apps?{q}")
    data = [a for a in body.get("data", []) if a["attributes"].get("bundleId") == bundle_id]
    if not data:
        die(f"App Store Connect に Bundle ID {bundle_id} のアプリが無い（App Store Connect → アプリ → ＋ で作成）")
    return data[0]["id"], data[0]["attributes"].get("name")


# ---------------------------------------------------------------- commands

def cmd_write_key():
    path = key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(p8_pem())
    os.chmod(path, 0o600)
    print(path)


def cmd_check(bundle_id):
    import re

    ok = True
    for name, pattern in (("ASC_KEY_ID", r"[A-Z0-9]{10}"),
                          ("ASC_ISSUER_ID", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
                          ("APPLE_TEAM_ID", r"[A-Z0-9]{10}")):
        v = env(name)
        good = re.fullmatch(pattern, v.strip()) is not None
        print(f"{name}: 形式 {'OK' if good else '不正'}" + ("（前後に空白あり。自動で除去する）" if v != v.strip() else ""))
        ok &= good
    p8_pem()
    print("ASC_KEY_P8_BASE64: PEM として読めた")
    if not ok:
        die("Secrets の形式が不正（上の行を参照）")

    aid, name = app_id(bundle_id)
    print(f"App Store Connect のアプリ: {name}（{bundle_id}）")
    status, _ = api("GET", "/v1/users?limit=1", tolerate=True)
    if status == 200:
        print("API キーの権限: Admin 相当（OK）")
    else:
        print(f"::warning::GET /v1/users -> HTTP {status}。API キーが Admin でない可能性（自動署名のプロファイル発行に Admin が必要）")


def cmd_next_build(bundle_id, platform):
    aid, _ = app_id(bundle_id)
    base = {"filter[app]": aid, "sort": "-uploadedDate", "limit": "200", "fields[builds]": "version"}
    q = dict(base, **{"filter[preReleaseVersion.platform]": PLATFORMS.get(platform, "IOS")})
    status, body = api("GET", "/v1/builds?" + urllib.parse.urlencode(q), tolerate=True)
    if status != 200:
        status, body = api("GET", "/v1/builds?" + urllib.parse.urlencode(base), tolerate=True)
    latest = 0
    if status == 200:
        for b in body.get("data", []):
            v = str(b["attributes"].get("version") or "").split(".")[0]
            if v.isdigit():
                latest = max(latest, int(v))
    else:
        print(f"::warning::ビルド一覧を取得できなかった（HTTP {status}）。1 から始める", file=sys.stderr)
    print(latest + 1)


def list_certs():
    out = []
    path = "/v1/certificates?" + urllib.parse.urlencode(
        {"limit": 200, "fields[certificates]": "certificateType,displayName,name,expirationDate,serialNumber"})
    while path:
        _, res = api("GET", path)
        out.extend(res.get("data", []))
        nxt = res.get("links", {}).get("next")
        path = nxt.replace(API, "") if nxt else None
    return out


def cmd_certs_list():
    certs = list_certs()
    print(f"証明書 {len(certs)} 件")
    print(f"{'id':<12} {'type':<20} {'expires':<12} name")
    for c in certs:
        a = c["attributes"]
        print(f"{c['id']:<12} {a.get('certificateType', ''):<20} {(a.get('expirationDate') or '')[:10]:<12} "
              f"{a.get('displayName') or a.get('name') or ''}")


def cmd_cert_create(cert_types, csr_path, out_path):
    """cert_types はカンマ区切り。先頭から順に試し、最初に作れたものを使う。"""
    with open(csr_path) as f:
        csr = f.read()
    types = [t for t in cert_types.split(",") if t]
    for i, cert_type in enumerate(types):
        last = i == len(types) - 1
        status, res = api("POST", "/v1/certificates",
                          {"data": {"type": "certificates",
                                    "attributes": {"certificateType": cert_type, "csrContent": csr}}},
                          tolerate=True)
        if status in (200, 201):
            a = res["data"]["attributes"]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(a["certificateContent"]))
            print(f"{cert_type} 証明書を作成: {res['data']['id']}（期限 {(a.get('expirationDate') or '')[:10]}）")
            return
        print(f"{cert_type}: 作成できない（HTTP {status}）")
        print(str(res)[:2000])
        if last:
            sys.exit(3)


def cmd_revoke_all():
    targets = [c for c in list_certs()
               if c["attributes"].get("certificateType") in MANAGED_CERT_TYPES
               and (c["attributes"].get("displayName") or "") == "Created via API"]
    if not targets:
        print("失効対象なし")
        return
    for c in targets:
        status, text = api("DELETE", f"/v1/certificates/{c['id']}", tolerate=True)
        print(f"{c['id']}: {'失効' if status in (200, 204) else f'失敗 HTTP {status}'}")


def password_candidates():
    """署名用秘密鍵のパスフレーズ候補。先頭が標準（新規暗号化に使う）。
    crosspoint の signing.yml は sha256(printf '%s' "$APPSTORE_P8") を使うので、改行の有無違いも候補に入れる。"""
    explicit = os.environ.get("SIGNING_KEY_PASSWORD", "")
    if explicit:
        return [explicit]
    pem = p8_pem()
    raw = p8_raw()
    crlf = pem.replace("\n", "\r\n")
    forms = [pem, pem.rstrip("\n"), raw, raw.strip(), raw.strip() + "\n",
             crlf, crlf.rstrip("\n"), crlf.rstrip("\r\n")]
    out = []
    for s in forms:
        h = hashlib.sha256(s.encode()).hexdigest()
        if h not in out:
            out.append(h)
    return out


def openssl(args, password):
    return subprocess.run(["openssl", *args, "-pass", "env:RW_PASS"],
                          env=dict(os.environ, RW_PASS=password), capture_output=True)


def cmd_decrypt_key(src, dst):
    for i, pw in enumerate(password_candidates()):
        r = openssl(["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-in", src, "-out", dst], pw)
        if r.returncode == 0:
            check = subprocess.run(["openssl", "pkey", "-in", dst, "-noout"], capture_output=True)
            if check.returncode == 0:
                print(f"署名用秘密鍵を復号した（候補 {i + 1}）")
                return
        if os.path.exists(dst):
            os.remove(dst)
    die("署名用秘密鍵を復号できない。.p8 が変わった可能性。runway の apple-signing を action=create で実行し直す")


def cmd_encrypt_key(src, dst):
    pw = password_candidates()[0]
    r = openssl(["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-in", src, "-out", dst], pw)
    if r.returncode != 0:
        print(r.stderr.decode(errors="replace"))
        die("暗号化に失敗")
    print(f"暗号化した: {dst}")


def cmd_upload(ipa, bundle_id, version, build, platform):
    size = os.path.getsize(ipa)
    print(f"{os.path.basename(ipa)}: {size} bytes, version {version} ({build}), platform {platform}")
    aid, _ = app_id(bundle_id)

    _, res = api("POST", "/v1/buildUploads", {"data": {
        "type": "buildUploads",
        "attributes": {"cfBundleShortVersionString": version, "cfBundleVersion": build,
                       "platform": PLATFORMS.get(platform, "IOS")},
        "relationships": {"app": {"data": {"type": "apps", "id": aid}}},
    }})
    upload = res["data"]
    print("build upload:", upload["id"])

    _, res = api("POST", "/v1/buildUploadFiles", {"data": {
        "type": "buildUploadFiles",
        "attributes": {"fileName": os.path.basename(ipa), "fileSize": size, "uti": "com.apple.ipa",
                       "assetType": "ASSET"},
        "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload["id"]}}},
    }})
    file = res["data"]
    operations = file["attributes"].get("uploadOperations") or []
    print(f"build upload file: {file['id']}, {len(operations)} part(s)")

    md5 = hashlib.md5()
    with open(ipa, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
        for n, op in enumerate(operations, 1):
            f.seek(op["offset"])
            part = f.read(op["length"])
            headers = {h["name"]: h["value"] for h in op.get("requestHeaders") or []}
            req = urllib.request.Request(op["url"], data=part, method=op["method"], headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    print(f"part {n}/{len(operations)}: HTTP {r.status}")
            except urllib.error.HTTPError as e:
                print(f"part {n}/{len(operations)}: HTTP {e.code}")
                print(e.read().decode(errors="replace")[:2000])
                sys.exit(1)

    api("PATCH", f"/v1/buildUploadFiles/{file['id']}", {"data": {
        "type": "buildUploadFiles", "id": file["id"],
        "attributes": {"uploaded": True,
                       "sourceFileChecksums": {"file": {"hash": md5.hexdigest(), "algorithm": "MD5"}}},
    }})
    print("アップロード完了。App Store Connect の処理を待つ")

    deadline = time.time() + 30 * 60
    last = None
    while time.time() < deadline:
        _, res = api("GET", f"/v1/buildUploads/{upload['id']}")
        state = res["data"]["attributes"].get("state") or {}
        if state.get("state") != last:
            last = state.get("state")
            print("state:", last)
        for kind in ("errors", "warnings", "infos"):
            for d in state.get(kind) or []:
                print(f"  {kind[:-1]}: {d.get('code')}: {d.get('description')}")
        if last == "COMPLETE":
            print("TestFlight に届いた（App Store Connect の TestFlight タブに数分で出る）")
            return
        if last == "FAILED":
            die("App Store Connect がビルドを拒否した（上のエラー参照）")
        time.sleep(20)
    print("::warning::30 分たっても処理中。App Store Connect の TestFlight タブを確認")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {
        "write-key": (cmd_write_key, 0), "check": (cmd_check, 1), "next-build": (cmd_next_build, 2),
        "certs-list": (cmd_certs_list, 0), "cert-create": (cmd_cert_create, 3), "revoke-all": (cmd_revoke_all, 0),
        "decrypt-key": (cmd_decrypt_key, 2), "encrypt-key": (cmd_encrypt_key, 2), "upload": (cmd_upload, 5),
    }
    if cmd not in table or len(args) != table[cmd][1]:
        print(__doc__)
        sys.exit(2)
    table[cmd][0](*args)


if __name__ == "__main__":
    main()
