#!/usr/bin/env python3
"""runway 用 App Store Connect API ツール（Python 3 + pyjwt + cryptography、署名鍵の暗号処理は openssl）。

API のエンドポイントとフィールドは Apple の App Store Connect API（OpenAPI 仕様）に準拠：
  GET /v1/apps, GET /v1/users, GET /v1/builds,
  GET/POST/DELETE /v1/certificates,
  GET/POST /v1/bundleIds, GET /v1/bundleIds/{id}/bundleIdCapabilities, POST /v1/bundleIdCapabilities,
  GET/POST/DELETE /v1/profiles,
  POST /v1/buildUploads, POST/PATCH /v1/buildUploadFiles, GET /v1/buildUploads/{id}
（アップロードと証明書の処理は kajisho5/crosspoint の .github/scripts/asc_upload.py / asc_certs.py で実績のあるもの）

使い方:
  asc.py write-key                         環境変数の .p8 を $RUNNER_TEMP/keys/AuthKey_<KEY_ID>.p8 に書き出す
  asc.py check <bundle id>                 鍵の形式・API 認証・アプリ記録・Admin 権限を確認
  asc.py next-build <bundle id> <platform> TestFlight 上の最大ビルド番号 + 1 を出力
  asc.py certs-list                        証明書一覧
  asc.py cert-create <TYPE[,TYPE...]> <csr.pem> <out.cer>   先頭から順に試す。全部ダメなら終了コード 3
  asc.py cert-find <csr.pem> <out.cer>      CSR と同じ公開鍵の証明書を Apple 側から探して書き出す（無ければ終了コード 4）
  asc.py revoke-all                        API で作った DEVELOPMENT / DISTRIBUTION 証明書をすべて失効
  asc.py decrypt-key <in.enc> <out.key>    署名用秘密鍵を復号（.p8 から導くパスフレーズ候補を順に試す）
  asc.py encrypt-key <in.key> <out.enc>    署名用秘密鍵を暗号化（標準パスフレーズ）
  asc.py upload <ipa> <bundle id> <version> <build> <platform>
  asc.py profile-devid <bundle id> <名前> <capability 種類[,種類…]> <devid.pem> <out.provisionprofile>
                                           Mac の Developer ID 配布用プロファイル（MAC_APP_DIRECT）を作る。
                                           Bundle ID が無ければ作り、capability を有効にしてから発行する

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


def cmd_cert_find(csr_path, out_path):
    """CSR と同じ公開鍵の証明書を Apple 側から探して DER で書き出す（サイトで手動発行した証明書の取り込み用）。
    見つからなければ終了コード 4。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with open(csr_path, "rb") as f:
        want = x509.load_pem_x509_csr(f.read()).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    path = "/v1/certificates?" + urllib.parse.urlencode(
        {"limit": 200, "fields[certificates]": "certificateType,certificateContent,expirationDate,name"})
    while path:
        _, res = api("GET", path)
        for c in res.get("data", []):
            a = c["attributes"]
            content = a.get("certificateContent")
            if not content:
                continue
            der = base64.b64decode(content)
            got = x509.load_der_x509_certificate(der).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            if got == want:
                with open(out_path, "wb") as f:
                    f.write(der)
                print(f"見つけた: {c['id']} {a.get('certificateType')}（期限 {(a.get('expirationDate') or '')[:10]}）")
                return
        nxt = res.get("links", {}).get("next")
        path = nxt.replace(API, "") if nxt else None
    print("CSR に対応する証明書は Apple 側にまだ無い")
    sys.exit(4)


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


def cmd_profile_devid(bundle_id, name, cap_types, pem_path, out_path):
    """Developer ID 配布用（MAC_APP_DIRECT）のプロファイルを作って書き出す。"""
    import re
    # Apple の決まり：App ID の名前は英数字と空白だけ
    name = re.sub(r"[^A-Za-z0-9 ]+", " ", name).strip()
    # 1) Bundle ID（Mac 用）。無ければ作る
    q = urllib.parse.urlencode({"filter[identifier]": bundle_id, "limit": 200})
    _, res = api("GET", f"/v1/bundleIds?{q}")
    found = [b for b in res.get("data", []) if b["attributes"].get("identifier") == bundle_id
             and b["attributes"].get("platform") in ("MAC_OS", "UNIVERSAL")]
    if found:
        bid = found[0]["id"]
        print(f"Bundle ID {bundle_id} はある（{found[0]['attributes'].get('platform')}）")
    else:
        _, res = api("POST", "/v1/bundleIds", {"data": {"type": "bundleIds", "attributes": {
            "identifier": bundle_id, "name": name, "platform": "MAC_OS"}}})
        bid = res["data"]["id"]
        print(f"Bundle ID {bundle_id} を作った")

    # 2) capability（候補を順に試す。名前が分からないものは Apple の返事を表示する）
    _, res = api("GET", f"/v1/bundleIds/{bid}/bundleIdCapabilities?limit=200")
    have = {c["attributes"].get("capabilityType") for c in res.get("data", [])}
    print("有効な capability:", ", ".join(sorted(x for x in have if x)) or "なし")
    wanted = [t.strip() for t in cap_types.split(",") if t.strip()]
    if wanted and not (have & set(wanted)):
        ok = False
        for t in wanted:
            status, text = api("POST", "/v1/bundleIdCapabilities", {"data": {
                "type": "bundleIdCapabilities", "attributes": {"capabilityType": t},
                "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bid}}}}}, tolerate=True)
            if isinstance(status, int) and 200 <= status < 300:
                print(f"capability {t} を有効にした")
                ok = True
                break
            print(f"capability {t} -> HTTP {status}: {str(text)[:1500]}")
        if not ok:
            die("capability を有効にできなかった（上の Apple の返事を参照）", 5)

    # 3) 署名に使う Developer ID 証明書を、シリアル番号で Apple 側から探す
    r = subprocess.run(["openssl", "x509", "-in", pem_path, "-noout", "-serial"], capture_output=True, text=True, check=True)
    serial = r.stdout.strip().split("=", 1)[1].upper().lstrip("0")
    cert = None
    for c in list_certs():
        sn = (c["attributes"].get("serialNumber") or "").upper().lstrip("0")
        if sn == serial:
            cert = c
            break
    if not cert:
        die(f"Developer ID 証明書（シリアル {serial}）が Apple 側に見つからない")
    print(f"証明書: {cert['attributes'].get('certificateType')} {cert['id']}")

    # 4) 同じ名前の古いプロファイルは消して作り直す（capability の変更を確実に反映するため）
    q = urllib.parse.urlencode({"filter[name]": name, "limit": 200})
    _, res = api("GET", f"/v1/profiles?{q}")
    for p in res.get("data", []):
        if p["attributes"].get("name") == name:
            api("DELETE", f"/v1/profiles/{p['id']}", tolerate=True)
            print(f"古いプロファイル {p['id']} を消した")
    _, res = api("POST", "/v1/profiles", {"data": {"type": "profiles",
        "attributes": {"name": name, "profileType": "MAC_APP_DIRECT"},
        "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bid}},
                          "certificates": {"data": [{"type": "certificates", "id": cert["id"]}]}}}})
    content = res["data"]["attributes"].get("profileContent")
    if not content:
        die("プロファイルの中身が返ってこなかった")
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(content))
    print(f"プロファイルを書き出した: {out_path}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {
        "write-key": (cmd_write_key, 0), "check": (cmd_check, 1), "next-build": (cmd_next_build, 2),
        "certs-list": (cmd_certs_list, 0), "cert-create": (cmd_cert_create, 3), "revoke-all": (cmd_revoke_all, 0),
        "cert-find": (cmd_cert_find, 2),
        "decrypt-key": (cmd_decrypt_key, 2), "encrypt-key": (cmd_encrypt_key, 2), "upload": (cmd_upload, 5),
        "profile-devid": (cmd_profile_devid, 5),
    }
    if cmd not in table or len(args) != table[cmd][1]:
        print(__doc__)
        sys.exit(2)
    table[cmd][0](*args)


if __name__ == "__main__":
    main()
