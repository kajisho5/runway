#!/usr/bin/env python3
"""runway 用 App Store Connect API ツール（Python 3 + pyjwt + cryptography、署名鍵の暗号処理は openssl）。

API のエンドポイントとフィールドは Apple の App Store Connect API（OpenAPI 仕様）に準拠：
  GET /v1/apps, GET /v1/users, GET /v1/builds,
  GET/POST/PATCH /v1/betaGroups, GET /v1/betaGroups/{id}/betaTesters, POST /v1/betaTesters, POST /v1/betaTesterInvitations,
  GET /v1/builds/{id}/betaBuildLocalizations, POST/PATCH /v1/betaBuildLocalizations,
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
  asc.py bundle-id <bundle id> <名前> <platform>   Bundle ID を登録（あれば何もしない）し、アプリ記録の有無を表示
  asc.py testers <bundle id>               内部テストのグループを用意（無ければ作る・全ビルド自動配信）し、
                                           アカウント所有者・管理者をテスターに入れ、まだ招待していなければ招待を送る
  asc.py whats-new <bundle id> <version> <build> <テキストファイル>
                                           そのビルドの「テストの内容」（TestFlight アプリに出る説明）を書き込む
  asc.py check-ids <bundle id>             Bundle ID の登録と App Store Connect のアプリ記録の有無を表示
  asc.py status <bundle id> <platform>     最新ビルドの処理・配信の状態と、テスターの状態を日本語で表示
                                           （GITHUB_STEP_SUMMARY があれば実行結果のまとめにも書く。メールアドレスは出さない）

環境変数:
  ASC_KEY_ID, ASC_ISSUER_ID
  ASC_KEY_P8_BASE64   .p8 の中身（PEM のまま、または base64）
  SIGNING_KEY_PASSWORD（任意）署名用秘密鍵のパスフレーズ。無ければ .p8 から導出
  RUNNER_TEMP
platform: ios | tvos | visionos | macos
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
PLATFORMS = {"ios": "IOS", "tvos": "TV_OS", "visionos": "VISION_OS", "macos": "MAC_OS"}
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
        "attributes": {"fileName": os.path.basename(ipa), "fileSize": size,
                       "uti": "com.apple.pkg" if ipa.endswith(".pkg") else "com.apple.ipa",
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
    _, res = api("GET", f"/v1/bundleIds/{bid}/bundleIdCapabilities")
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
            # App Store Connect API で有効にできない権限もある（FSKit など）。その場合は Web の Identifiers で有効にしてもらう
            print(f"::warning::capability {cap_types} は API では有効にできなかった。"
                  "Apple Developer の Identifiers で手で有効にしてあれば、そのまま発行できる")

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


def cmd_bundle_id(bundle_id, name, platform):
    """Bundle ID を登録する（無ければ作る）。App Store Connect のアプリ記録は API では作れないので有無だけ表示する。"""
    import re
    name = re.sub(r"[^A-Za-z0-9 ]+", " ", name).strip() or "App"   # Apple の決まり：英数字と空白だけ
    plat = {"ios": "IOS", "tvos": "IOS", "visionos": "IOS", "macos": "MAC_OS", "universal": "UNIVERSAL"}.get(platform.lower())
    if not plat:
        die(f"platform は ios / tvos / visionos / macos / universal（指定値: {platform}）")
    q = urllib.parse.urlencode({"filter[identifier]": bundle_id, "limit": 200})
    _, res = api("GET", f"/v1/bundleIds?{q}")
    found = [b for b in res.get("data", []) if b["attributes"].get("identifier") == bundle_id]
    if found:
        print(f"Bundle ID {bundle_id} は登録済み（{found[0]['attributes'].get('platform')}）")
    else:
        _, res = api("POST", "/v1/bundleIds", {"data": {"type": "bundleIds", "attributes": {
            "identifier": bundle_id, "name": name, "platform": plat}}})
        print(f"Bundle ID {bundle_id} を登録した（{plat}）")
    q = urllib.parse.urlencode({"filter[bundleId]": bundle_id})
    _, res = api("GET", f"/v1/apps?{q}")
    if res.get("data"):
        print(f"App Store Connect のアプリ記録：あり（{res['data'][0]['attributes'].get('name')}）")
    else:
        print("::warning::App Store Connect のアプリ記録がまだ無い。API では作れないので、"
              "https://appstoreconnect.apple.com/apps の ＋ → 新規App で作る")


# ---------------------------------------------------------------- TestFlight の配信先・状態

ASC_WEB = "https://appstoreconnect.apple.com"
TF_PATH = {"ios": "ios", "tvos": "tvos", "visionos": "visionos", "macos": "macos"}

PROCESSING_JA = {
    "PROCESSING": "Apple が処理中（数分〜30 分ほど待つ）",
    "VALID": "処理済み",
    "INVALID": "Apple が受け付けなかった（Apple から届くメールの ITMS- で始まる番号に原因が書いてある）",
    "FAILED": "処理に失敗した",
}
INTERNAL_JA = {
    "PROCESSING": "処理中",
    "PROCESSING_EXCEPTION": "処理で問題が出た（App Store Connect の TestFlight 画面を確認）",
    "MISSING_EXPORT_COMPLIANCE": "輸出コンプライアンス（暗号化の質問）が未回答。Info.plist に ITSAppUsesNonExemptEncryption = NO を入れて出し直すか、TestFlight 画面で回答する",
    "READY_FOR_BETA_TESTING": "配れる状態だが、まだ誰にも配られていない（内部テストのグループが無い／自動配信がオフ）",
    "IN_BETA_TESTING": "内部テスターに配信中（TestFlight アプリに出る）",
    "EXPIRED": "期限切れ（90 日）",
    "IN_EXPORT_COMPLIANCE_REVIEW": "輸出コンプライアンスの審査中",
}
TESTER_JA = {
    "NOT_INVITED": "まだ招待していない",
    "INVITED": "招待済み・未承認（App Store Connect のメールアドレスに届いた招待メールを開いて承認する）",
    "ACCEPTED": "承認済み",
    "INSTALLED": "インストール済み",
    "REVOKED": "取り消し済み",
}


def find_app(bundle_id):
    """app_id と違い、見つからなくても止まらない（None を返す）。"""
    q = urllib.parse.urlencode({"filter[bundleId]": bundle_id, "fields[apps]": "bundleId,name"})
    status, body = api("GET", f"/v1/apps?{q}", tolerate=True)
    if status != 200:
        print(f"::warning::アプリの検索に失敗（HTTP {status}）")
        return None, None
    data = [a for a in body.get("data", []) if a["attributes"].get("bundleId") == bundle_id]
    return (data[0]["id"], data[0]["attributes"].get("name")) if data else (None, None)


def get_all(path):
    out = []
    while path:
        status, res = api("GET", path, tolerate=True)
        if status != 200:
            print(f"::warning::GET {path.split('?')[0]} -> HTTP {status}")
            return out
        out.extend(res.get("data", []))
        nxt = res.get("links", {}).get("next")
        path = nxt.replace(API, "") if nxt else None
    return out


def internal_group(aid, create):
    groups = get_all(f"/v1/apps/{aid}/betaGroups?limit=200")
    found = [g for g in groups if g["attributes"].get("isInternalGroup")]
    if found:
        return found[0], False
    if not create:
        return None, False
    status, res = api("POST", "/v1/betaGroups", {"data": {
        "type": "betaGroups",
        "attributes": {"name": "Internal", "isInternalGroup": True, "hasAccessToAllBuilds": True},
        "relationships": {"app": {"data": {"type": "apps", "id": aid}}}}}, tolerate=True)
    if status >= 300:
        print(f"::warning::内部テストのグループを作れなかった（HTTP {status}）。"
              f"手でやるなら App Store Connect → アプリ → TestFlight → 内部テスト の ＋")
        print(str(res)[:1500])
        return None, False
    return res["data"], True


def cmd_testers(bundle_id):
    aid, name = find_app(bundle_id)
    if not aid:
        print(f"::warning::App Store Connect に {bundle_id} のアプリが無いので、テスターの準備を飛ばした")
        return
    group, created = internal_group(aid, create=True)
    if not group:
        return
    gname = group["attributes"].get("name")
    print(f"内部テストのグループ「{gname}」を{'作った（全ビルド自動配信）' if created else '確認した'}")
    if not group["attributes"].get("hasAccessToAllBuilds"):
        status, _ = api("PATCH", f"/v1/betaGroups/{group['id']}", {"data": {
            "type": "betaGroups", "id": group["id"], "attributes": {"hasAccessToAllBuilds": True}}}, tolerate=True)
        print("全ビルドの自動配信をオンにした" if status < 300 else f"::warning::自動配信をオンにできなかった（HTTP {status}）")

    existing = get_all(f"/v1/betaGroups/{group['id']}/betaTesters?limit=200")
    emails = {(t["attributes"].get("email") or "").lower() for t in existing}
    users = get_all("/v1/users?limit=200")
    added = 0
    for u in users:
        a = u["attributes"]
        email = (a.get("username") or "").lower()
        if not ({"ACCOUNT_HOLDER", "ADMIN"} & set(a.get("roles") or [])) or not email or email in emails:
            continue
        status, res = api("POST", "/v1/betaTesters", {"data": {
            "type": "betaTesters",
            "attributes": {"email": email, "firstName": a.get("firstName") or "", "lastName": a.get("lastName") or ""},
            "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group["id"]}]}}}}, tolerate=True)
        if status < 300:
            added += 1
        else:
            print(f"::warning::テスターを追加できなかった（HTTP {status}）")
    print(f"テスター：もともと {len(emails)} 人、今回追加 {added} 人（管理者・アカウント所有者）")

    invited = 0
    for t in get_all(f"/v1/betaGroups/{group['id']}/betaTesters?limit=200"):
        # 毎回のビルドで招待メールが何通も届かないよう、まだ招待していない人にだけ送る
        if t["attributes"].get("state") not in ("NOT_INVITED", None, ""):
            continue
        status, _ = api("POST", "/v1/betaTesterInvitations", {"data": {
            "type": "betaTesterInvitations",
            "relationships": {"app": {"data": {"type": "apps", "id": aid}},
                              "betaTester": {"data": {"type": "betaTesters", "id": t["id"]}}}}}, tolerate=True)
        invited += status < 300
    if invited:
        print(f"テスター {invited} 人に招待メールを送った")


def find_build(aid, version, build, wait):
    q = urllib.parse.urlencode({"filter[app]": aid, "filter[version]": build,
                                "filter[preReleaseVersion.version]": version, "limit": 5})
    deadline = time.time() + wait
    while True:
        status, res = api("GET", f"/v1/builds?{q}", tolerate=True)
        if status == 200 and res.get("data"):
            return res["data"][0]["id"]
        if time.time() >= deadline:
            return None
        time.sleep(20)


def cmd_whats_new(bundle_id, version, build, text_file):
    with open(text_file, encoding="utf-8") as f:
        text = f.read().strip()[:4000]
    if not text:
        print("テストの内容が空なので書き込まない")
        return
    aid, _ = find_app(bundle_id)
    bid = find_build(aid, version, build, wait=5 * 60) if aid else None
    if not bid:
        print(f"::warning::ビルド {version} ({build}) がまだ見つからないので、テストの内容を書き込めなかった")
        return
    status, res = api("GET", f"/v1/builds/{bid}/betaBuildLocalizations?limit=50", tolerate=True)
    locs = res.get("data", []) if status == 200 else []
    if locs:
        ok = 0
        for loc in locs:
            st, _ = api("PATCH", f"/v1/betaBuildLocalizations/{loc['id']}", {"data": {
                "type": "betaBuildLocalizations", "id": loc["id"], "attributes": {"whatsNew": text}}}, tolerate=True)
            ok += st < 300
        print(f"テストの内容を書き込んだ（{ok}/{len(locs)} 言語）")
        return
    for locale in ("ja", "en-US"):
        st, _ = api("POST", "/v1/betaBuildLocalizations", {"data": {
            "type": "betaBuildLocalizations", "attributes": {"locale": locale, "whatsNew": text},
            "relationships": {"build": {"data": {"type": "builds", "id": bid}}}}}, tolerate=True)
        if st < 300:
            print(f"テストの内容を書き込んだ（{locale}）")
            return
    print("::warning::テストの内容を書き込めなかった")


def cmd_status(bundle_id, platform):
    lines = []

    def out(line=""):
        print(line)
        lines.append(line)

    aid, name = find_app(bundle_id)
    if not aid:
        out(f"**App Store Connect に `{bundle_id}` のアプリが無い。**")
        out(f"{ASC_WEB}/apps の「＋」→「新規App」で、この Bundle ID のアプリを作る。")
        write_summary(lines)
        return
    tf = f"{ASC_WEB}/apps/{aid}/testflight/{TF_PATH.get(platform, 'ios')}"
    out(f"### {name}（`{bundle_id}`）")
    out(f"TestFlight の画面: {tf}")
    out()

    q = urllib.parse.urlencode({"filter[app]": aid, "sort": "-uploadedDate", "limit": 5,
                                "include": "buildBetaDetail,preReleaseVersion"})
    status, res = api("GET", f"/v1/builds?{q}", tolerate=True)
    builds = res.get("data", []) if status == 200 else []
    inc = {(i["type"], i["id"]): i["attributes"] for i in (res.get("included", []) if status == 200 else [])}
    advice = []
    if not builds:
        out("ビルドはまだ 1 つも届いていない。アプリのリポの Actions → testflight → Run workflow で送る。")
    else:
        out("| バージョン | 届いた日時 | Apple の処理 | 配信 |")
        out("|---|---|---|---|")
        for i, b in enumerate(builds):
            a, rel = b["attributes"], b.get("relationships", {})
            det = inc.get(("buildBetaDetails", (rel.get("buildBetaDetail", {}).get("data") or {}).get("id")), {})
            pre = inc.get(("preReleaseVersions", (rel.get("preReleaseVersion", {}).get("data") or {}).get("id")), {})
            ps, ib = a.get("processingState") or "", det.get("internalBuildState") or ""
            when = (a.get("uploadedDate") or "")[:16].replace("T", " ")
            out(f"| {pre.get('version', '?')} ({a.get('version')}) | {when} UTC | "
                f"{PROCESSING_JA.get(ps, ps)} | {INTERNAL_JA.get(ib, ib or '-')} |")
            if i == 0:
                if ps in ("INVALID", "FAILED"):
                    advice.append(PROCESSING_JA[ps])
                elif ib in ("MISSING_EXPORT_COMPLIANCE", "READY_FOR_BETA_TESTING", "PROCESSING_EXCEPTION", "EXPIRED"):
                    advice.append(INTERNAL_JA[ib])
                elif ps == "PROCESSING" or ib == "PROCESSING":
                    advice.append("最新のビルドは Apple が処理中。数分〜30 分で TestFlight に出る")
    out()

    group, _ = internal_group(aid, create=False)
    if not group:
        out("**内部テストのグループが無い。** ビルドが届いても誰にも配られない。")
        advice.append("内部テストのグループが無い（runway の Actions → testflight-check を「直す」をオンで実行すると作る）")
    else:
        ga = group["attributes"]
        testers = get_all(f"/v1/betaGroups/{group['id']}/betaTesters?limit=200")
        out(f"内部テストのグループ「{ga.get('name')}」：全ビルド自動配信 {'オン' if ga.get('hasAccessToAllBuilds') else '**オフ**'}、"
            f"テスター {len(testers)} 人")
        if not ga.get("hasAccessToAllBuilds"):
            advice.append("自動配信がオフ（testflight-check を「直す」で実行するとオンにする）")
        if not testers:
            advice.append("テスターが 0 人（testflight-check を「直す」で実行すると管理者を入れる）")
        for n, t in enumerate(testers, 1):
            st = t["attributes"].get("state") or ""
            out(f"- テスター {n}: {TESTER_JA.get(st, st)}")
            if st == "INVITED":
                advice.append(TESTER_JA["INVITED"] + "。iPhone の TestFlight アプリが同じ Apple ID でサインインしているかも確認")
    out()
    if advice:
        out("#### 次にやること")
        for a in dict.fromkeys(advice):
            out(f"- {a}")
    else:
        out("問題は見つからなかった。TestFlight アプリに出ていなければ、アプリを開き直す。")
    write_summary(lines)


def cmd_check_ids(bundle_id):
    """Bundle ID の登録とアプリ記録の有無を、直し方の URL つきで表示する（止まらない）。"""
    lines = []
    q = urllib.parse.urlencode({"filter[identifier]": bundle_id, "limit": 200})
    status, res = api("GET", f"/v1/bundleIds?{q}", tolerate=True)
    if status != 200:
        lines.append(f"- Bundle ID：確認できなかった（HTTP {status}。API キーの権限を確認）")
    elif any(b["attributes"].get("identifier") == bundle_id for b in res.get("data", [])):
        lines.append(f"- Bundle ID `{bundle_id}`：登録済み")
    else:
        lines.append(f"- **Bundle ID `{bundle_id}` が未登録。** runway の Actions → apple-bundle-id で登録する"
                     "（手でやるなら https://developer.apple.com/account/resources/identifiers/add/bundleId ）")
    aid, name = find_app(bundle_id)
    if aid:
        lines.append(f"- App Store Connect のアプリ記録：あり（{name}）")
    else:
        lines.append(f"- **App Store Connect のアプリ記録が無い。** {ASC_WEB}/apps の「＋」→「新規App」で作る"
                     "（Apple が API での作成を許していないので、ここだけは手作業）")
    lines.append("")
    for line in lines:
        print(line)
    write_summary(lines)


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


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
        "profile-devid": (cmd_profile_devid, 5), "bundle-id": (cmd_bundle_id, 3),
        "testers": (cmd_testers, 1), "check-ids": (cmd_check_ids, 1), "whats-new": (cmd_whats_new, 4), "status": (cmd_status, 2),
    }
    if cmd not in table or len(args) != table[cmd][1]:
        print(__doc__)
        sys.exit(2)
    table[cmd][0](*args)


if __name__ == "__main__":
    main()
