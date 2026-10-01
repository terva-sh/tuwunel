#!/usr/bin/env python3
"""Sign-in through an OpenID Connect provider, end to end, on loopback.

  sso_check.py --binary PATH --work DIR [--port PORT]

A stand-in provider runs in this process over HTTPS under a test CA, and a
proxied-mode server trusts that CA and names the provider through
`bundle.py idp`. This script plays the browser and the Matrix client:

- legacy SSO (`m.login.sso`, then `m.login.token`);
- next-gen auth (dynamic registration, authorization code with PKCE, the
  approval page, the token endpoint).

It checks that:

- signing in first grants nothing, and an operator is promoted by a startup
  admin command;
- a provider user named inside the appservice's namespace gets no login token,
  over either flow, with the provider untrusted and then trusted;
- `--no-registration` admits existing accounts only.

DIR must be new and outside Git. Everything generated is test-only and stays
there. Exit status is nonzero on any failed assertion. Ticket
TKT-01M3TZ9T038D4HZ3TPPWQG9AF3 (OIDC sign-in for the bundled profile).
"""

import argparse
import base64
import hashlib
import html
import http.client
import http.server
import json
import re
import secrets
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import bundle

PREFIX = bundle.PREFIX
SERVER, PUBLIC = "agents.test", "matrix.agents.test"
CLIENT_ID = "agents-matrix"
CLIENT_SITE = "https://client.agents.test"


def challenge_of(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class Provider(http.server.ThreadingHTTPServer):
    """The provider side of the flow, strict where the real one is: exact
    redirect URI, client secret, single-use codes, and S256 PKCE."""

    def __init__(self, port, secret, callback):
        super().__init__(("127.0.0.1", port), ProviderHandler)
        self.issuer = f"https://127.0.0.1:{port}/"
        self.secret, self.callback = secret, callback
        self.next_user = None
        self.codes, self.tokens = {}, {}
        self.refusals = []

    def refuse(self, reason):
        self.refusals.append(reason)
        return reason


class ProviderHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body=None, location=None):
        payload = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        idp, url = self.server, urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/.well-known/openid-configuration":
            return self.reply(200, {
                "issuer": idp.issuer, "authorization_endpoint": idp.issuer + "authorize",
                "token_endpoint": idp.issuer + "token", "userinfo_endpoint": idp.issuer + "userinfo",
                "jwks_uri": idp.issuer + "jwks", "response_types_supported": ["code"],
                "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile"], "code_challenge_methods_supported": ["S256"]})
        if url.path == "/authorize":
            if query.get("client_id") != CLIENT_ID or query.get("redirect_uri") != idp.callback:
                return self.reply(400, {"error": idp.refuse(f"authorize: client or redirect {query.get('redirect_uri')}")})
            if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
                return self.reply(400, {"error": idp.refuse("authorize: no S256 challenge")})
            code = secrets.token_urlsafe(16)
            idp.codes[code] = (idp.next_user, query["code_challenge"])
            return self.reply(302, location=f"{idp.callback}?{urlencode({'code': code, 'state': query.get('state', '')})}")
        if url.path == "/userinfo":
            user = idp.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
            if not user:
                return self.reply(401, {"error": idp.refuse("userinfo: unknown token")})
            return self.reply(200, {"sub": f"sub-{user}", "preferred_username": user, "name": user.title(),
                                    "email": f"{user}@{SERVER}"})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        idp = self.server
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if self.path != "/token":
            return self.reply(404, {"error": "not found"})
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != idp.secret:
            return self.reply(401, {"error": idp.refuse("token: client authentication")})
        if form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != idp.callback:
            return self.reply(400, {"error": idp.refuse("token: grant or redirect")})
        user, challenge = idp.codes.pop(form.get("code", ""), (None, None))
        if not user:
            return self.reply(400, {"error": idp.refuse("token: unknown or reused code")})
        if challenge_of(form.get("code_verifier", "")) != challenge:
            return self.reply(400, {"error": idp.refuse("token: PKCE verifier")})
        token = secrets.token_urlsafe(24)
        idp.tokens[token] = user
        return self.reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": 300})


class Browser:
    """Follows the flow by hand: the server's public URLs are mapped to its
    loopback listener, the provider is reached over HTTPS, and the grant cookie
    is carried between the two hops that need it."""

    def __init__(self, port, cafile):
        self.port, self.context = port, ssl.create_default_context(cafile=cafile)
        self.cookies = {}

    def request(self, method, url, body=None, headers=None):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.hostname == PUBLIC or not parts.scheme:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            headers = {**(headers or {}), "Host": PUBLIC, "X-Forwarded-Proto": "https"}
            if self.cookies:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        else:
            conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=30, context=self.context)
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        for value in response.headers.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            self.cookies[name.strip()] = rest.partition(";")[0]
        return response.status, response.headers.get("Location"), payload.decode("utf-8", "replace")

    def follow(self, url, hops=8):
        """GETs and follows redirects; returns the last response and the URL
        that produced it. Stops at a redirect off both servers."""
        for _ in range(hops):
            status, location, body = self.request("GET", url)
            if status not in (301, 302, 303, 307, 308) or not location:
                return status, url, body
            target = urlsplit(location)
            if target.hostname not in (PUBLIC, "127.0.0.1"):
                return status, location, body
            url = location
        return 0, url, "too many redirects"

    def json(self, method, url, body=None, token=None, form=False):
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = urlencode(body) if form else (json.dumps(body) if body is not None else None)
        status, _, text = self.request(method, url, payload, headers)
        try:
            return status, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return status, {"raw": text[:200]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--port", type=int, default=18508, help="server port; the provider takes the next one")
    args = parser.parse_args(argv)
    work = bundle.safe_root(args.work)
    bundle.require(not work.exists(), "--work must be a new directory")
    work.mkdir(mode=0o700)

    ca = bundle.ca_init(work / "ca", "SSO check CA")
    (work / "provider").mkdir(mode=0o700)
    bundle.ca_issue(ca, "provider.test", work / "provider")
    root = work / "server"
    bundle.require(bundle.main(["init", str(root), "--binary", args.binary, "--server-name", SERVER,
                                "--mode", "proxied", "--public-host", PUBLIC, "--port", str(args.port),
                                "--trust", str(ca / "ca.crt")]) == 0, "init failed")
    secret = secrets.token_urlsafe(32)
    secret_file = root / "config" / "oidc-client-secret"
    secret_file.touch(mode=0o600)
    secret_file.write_text(secret + "\n")

    callback = f"https://{PUBLIC}/_matrix/client/unstable/login/sso/callback/{CLIENT_ID}"
    provider = Provider(args.port + 1, secret, callback)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(work / "provider" / "server.crt", work / "provider" / "server.key")
    provider.socket = tls.wrap_socket(provider.socket, server_side=True)
    threading.Thread(target=provider.serve_forever, daemon=True).start()

    def configure(*extra):
        bundle.require(bundle.main(["idp", str(root), "--issuer-url", provider.issuer, "--client-id", CLIENT_ID,
                                    *extra]) == 0, "idp failed")

    configure()
    loaded = bundle.load(root)
    checker = bundle.Check(*loaded)
    browser = Browser(args.port, str(ca / "ca.crt"))

    def record(name, condition=True, **details):
        checker.results.append({"name": name, "ok": bool(condition), **details})
        print(("ok   " if condition else "FAIL ") + name + (f"  {details}" if details and not condition else ""))
        return condition

    def sso_login(user):
        """Legacy SSO as `user`. Returns (status, user_id, access token, whether
        the server issued a login token). A refusal must come before the login
        token: /login has its own namespace check, and next-gen auth does not
        go through /login."""
        provider.next_user, browser.cookies = user, {}
        start = f"{PREFIX}/login/sso/redirect/{CLIENT_ID}?redirectUrl={quote(CLIENT_SITE + '/done', safe='')}"
        status, url, body = browser.follow(start)
        token = parse_qs(urlsplit(url).query).get("loginToken", [""])[0] if url.startswith(CLIENT_SITE) else ""
        if not token:
            return status, None, None, False
        status, data = browser.json("POST", f"{PREFIX}/login", {"type": "m.login.token", "token": token})
        return status, data.get("user_id"), data.get("access_token"), True

    def exists(localpart):
        status, _ = checker.api("GET", f"{PREFIX}/profile/{quote(f'@{localpart}:{SERVER}', safe='')}", auth=False)
        return status == 200

    def is_admin(token):
        # Server admin in tuwunel is joined membership of the admin room, which
        # the server creates at startup as #admins: user_is_admin
        # (src/service/admin/mod.rs) checks exactly that, and make_user_admin
        # joins the user to it. There is no separate flag to read.
        status, data = checker.api("GET", f"{PREFIX}/directory/room/{quote(f'#admins:{SERVER}', safe='')}", auth=False)
        room = data.get("room_id")
        status, joined = checker.api("GET", f"{PREFIX}/joined_rooms", token=token)
        return bool(room) and room in joined.get("joined_rooms", [])

    def next_gen_login(user, refused=False):
        """Next-gen auth as `user`. Returns the whoami user_id or None. With
        refused, a stop along the way is the expected outcome, not a failure."""
        record_step = (lambda *a, **k: False) if refused else record
        provider.next_user, browser.cookies = user, {}
        status, meta = browser.json("GET", "/_matrix/client/v1/auth_metadata")
        if status != 200:
            return record_step("next-gen: metadata", False, status=status) or None
        status, client = browser.json("POST", meta["registration_endpoint"], {
            "client_name": "sso check", "client_uri": CLIENT_SITE + "/", "redirect_uris": [CLIENT_SITE + "/cb"],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "application_type": "web"})
        if status not in (200, 201):
            return record_step("next-gen: registers a client", False, status=status, error=client) or None
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(8)
        query = urlencode({"response_type": "code", "client_id": client["client_id"],
                           "redirect_uri": CLIENT_SITE + "/cb", "state": state,
                           "scope": "openid urn:matrix:client:api:*", "code_challenge": challenge_of(verifier),
                           "code_challenge_method": "S256"})
        status, url, page = browser.follow(f"{meta['authorization_endpoint']}?{query}")
        fields = dict(re.findall(r'name="(oidc_req_id|loginToken)" value="([^"]*)"', page))
        if len(fields) != 2:
            return record_step("next-gen: reaches the approval page", False, status=status, url=url[:120]) or None
        form = {k: html.unescape(v) for k, v in fields.items()} | {"action": "approve"}
        status, _, page = browser.request("POST", "/_tuwunel/oidc/_complete", urlencode(form),
                                          {"Content-Type": "application/x-www-form-urlencoded"})
        found = re.search(r"https://client\.agents\.test/cb\?[^\"'<\s]*", html.unescape(page))
        returned = parse_qs(urlsplit(found.group(0)).query) if found else {}
        if returned.get("state", [""])[0] != state or "code" not in returned:
            return record_step("next-gen: approval returns a code", False, status=status) or None
        status, tokens = browser.json("POST", meta["token_endpoint"], {
            "grant_type": "authorization_code", "code": returned["code"][0], "redirect_uri": CLIENT_SITE + "/cb",
            "client_id": client["client_id"], "code_verifier": verifier}, form=True)
        if status != 200:
            return record_step("next-gen: token exchange", False, status=status, error=tokens) or None
        status, me = browser.json("GET", f"{PREFIX}/account/whoami", token=tokens.get("access_token"))
        return me.get("user_id") if status == 200 else None

    try:
        bundle.require(checker.start(), "server did not start")

        # Provider untrusted, registration on.
        # The bundle profiles set grant_admin_to_first_user = false, so signing
        # in first confers nothing; operators are promoted by admin command.
        status, user, operator_token, _ = sso_login("operator")
        record("legacy SSO: first user signs in", user == f"@operator:{SERVER}", status=status, user=user)
        record("first account is not made admin (profile policy)", operator_token and not is_admin(operator_token))
        status, user, _, _ = sso_login("alice")
        record("legacy SSO: second user signs in", user == f"@alice:{SERVER}", status=status, user=user)
        status, user, _, _ = sso_login("alice")
        record("legacy SSO: same user returns to the same account", user == f"@alice:{SERVER}", user=user)
        status, user, _, issued = sso_login("terva_mallory")
        record("namespace: provider user inside it gets no login token", not issued, status=status, user=user)
        record("namespace: no account created inside it", not exists("terva_mallory"))
        record("next-gen: signs in through the provider", next_gen_login("bob") == f"@bob:{SERVER}")
        user = next_gen_login("terva_eve", refused=True)
        record("next-gen namespace: provider user inside it is refused", user is None, user=user)
        record("next-gen namespace: no account created inside it", not exists("terva_eve"))

        # Promote the operator the way the deployment record says: stop, run
        # the admin command at startup, let it shut down, start again.
        checker.stop()
        promote = subprocess.run(bundle.command(root, loaded[2]) + [
            "--execute", f"users make_user_admin @operator:{SERVER}", "--execute", "server shutdown"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        record("operator promoted by startup command", promote.returncode == 0, exit=promote.returncode)
        bundle.require(checker.start(), "server did not restart after promotion")
        record("operator is now an admin", is_admin(operator_token))

        agent = "terva_agent"
        status, data = checker.api("POST", f"{PREFIX}/register",
                                   {"type": "m.login.application_service", "username": agent, "inhibit_login": True})
        record("appservice provisions an agent", status == 200, status=status)

        # Provider trusted: it may match existing accounts, but never an agent.
        checker.stop()
        idp_file = root / "config" / bundle.IDP_FILE
        idp_file.write_text(idp_file.read_text().replace("trusted = false", "trusted = true"))
        bundle.require(checker.start(), "server did not restart trusted")
        status, user, _, issued = sso_login(agent)
        record("trusted: provider user named as an agent gets no login token", not issued, status=status, user=user)
        user = next_gen_login(agent, refused=True)
        record("trusted: next-gen as an agent does not reach the agent", user != f"@{agent}:{SERVER}", user=user)
        status, user, _, _ = sso_login("alice")
        record("trusted: existing human account still signs in", user == f"@alice:{SERVER}", user=user)

        # Registration off: existing accounts only.
        checker.stop()
        configure("--no-registration")
        bundle.require(checker.start(), "server did not restart without registration")
        status, user, _, issued = sso_login("carol")
        record("no-registration: unknown user gets no login token", not issued, status=status, user=user)
        record("no-registration: no account created", not exists("carol"))
        status, user, _, _ = sso_login("alice")
        record("no-registration: existing account signs in", user == f"@alice:{SERVER}", user=user)

        record("provider saw no malformed request", not provider.refusals, refusals=provider.refusals)
        record("secret never left the secret file", secret not in idp_file.read_text()
               and secret not in (root / "bundle.json").read_text())
    finally:
        checker.stop()
        provider.shutdown()

    failed = [r["name"] for r in checker.results if not r["ok"]]
    print(f"{'PASS' if not failed else 'FAIL'}: {len(checker.results) - len(failed)}/{len(checker.results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
