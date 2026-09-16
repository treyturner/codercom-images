import base64
import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import selectors
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HELPER_DIR = Path(os.environ.get("HOPPSCOTCH_TEST_HELPER_DIR", Path(__file__).resolve().parents[2] / "hoppscotch"))
NODE = "/opt/hoppscotch-mcp/bin/node"
MCP = "/opt/hoppscotch-mcp/node_modules/@hoppscotch/mcp-server/dist/index.js"
spec = importlib.util.spec_from_file_location("credentials", HELPER_DIR / "credentials.py")
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


def jwt(server, subject="user-one", ttl=3600, **extra):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((encode({"alg": "HS256"}), encode({
        "sub": subject, "iss": server, "aud": [server], "exp": int(time.time()) + ttl, **extra,
    }), "test-signature"))


def session(server, subject="user-one", ttl=3600, refresh_ttl=86400):
    return {"apiUrl": server + "/backend", "apiType": "selfhost",
            "accessToken": jwt(server, subject, ttl),
            "refreshToken": jwt(server, subject, refresh_ttl)}


class FakeBackend:
    def __init__(self, cert, key):
        self.refreshes = 0
        self.calls = []
        self.cookies = []
        self.response_status = 200
        self.response_subject = "user-one"
        self.access_ttl = 3600
        self.omit_cookie = False
        self.delay = 0
        self.expected_refresh = None
        backend = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/backend/v1/auth/refresh":
                    self.send_error(404)
                    return
                backend.refreshes += 1
                backend.cookies.append(dict(self.headers))
                if self.headers.get("Cookie") != "refresh_token=" + backend.expected_refresh:
                    self.send_error(403)
                    return
                time.sleep(backend.delay)
                self.send_response(backend.response_status)
                if backend.response_status == 302:
                    self.send_header("Location", backend.server + "/credential-leak")
                elif backend.response_status == 200:
                    backend.new_session = session(backend.server, backend.response_subject, ttl=backend.access_ttl)
                    backend.new_session["refreshToken"] = jwt(backend.server, backend.response_subject, ttl=86400, rotation=backend.refreshes)
                    # Include an Expires comma to exercise real Set-Cookie parsing.
                    self.send_header("Set-Cookie", "access_token=" + backend.new_session["accessToken"] + "; HttpOnly; Secure; Expires=Wed, 01 Jan 2031 00:00:00 GMT")
                    if not backend.omit_cookie:
                        self.send_header("Set-Cookie", "refresh_token=" + backend.new_session["refreshToken"] + "; HttpOnly; Secure")
                    backend.expected_refresh = backend.new_session["refreshToken"]
                self.end_headers()
                if backend.response_status != 200:
                    self.wfile.write(b"secret-looking-body-must-not-be-logged")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                backend.calls.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":{"rootRESTUserCollections":[]}}')

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.server = f"https://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificate_dir = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.certificate_dir.name) / "cert.pem"
        cls.key = Path(cls.certificate_dir.name) / "key.pem"
        subprocess.run([
            "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.certificate_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.backend = FakeBackend(self.cert, self.key)
        self.directory = self.home / ".config/hoppscotch-mcp-helper"
        self.store = credentials.Credentials(self.backend.server, self.directory)
        self.env = {**os.environ, "HOME": str(self.home), "SSL_CERT_FILE": str(self.cert),
                    "NODE_EXTRA_CA_CERTS": str(self.cert), "NO_PROXY": "127.0.0.1,localhost",
                    "HOPPSCOTCH_SERVER_URL": self.backend.server,
                    "HOPPSCOTCH_CREDENTIAL_HELPER": "true"}
        self.env.pop("HOPPSCOTCH_ACCESS_TOKEN", None)
        self.tls_env = mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.cert), "NO_PROXY": "127.0.0.1,localhost"})
        self.tls_env.start()

    def tearDown(self):
        self.tls_env.stop()
        self.backend.close()
        self.temp.cleanup()

    def seed(self, **kwargs):
        data = session(self.backend.server, **kwargs)
        self.backend.expected_refresh = data["refreshToken"]
        self.store.import_session(data)
        return self.store.read()

    def cli(self, command, input=None):
        return subprocess.run([sys.executable, "-I", str(HELPER_DIR / "credentials.py"), command],
                              input=input, env=self.env, text=True, capture_output=True, timeout=30)

    def test_import_is_private_and_diagnostics_do_not_print_tokens(self):
        data = session(self.backend.server)
        imported = self.cli("import", json.dumps(data))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "credentials.json").stat().st_mode & 0o777, 0o600)
        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        for output in (imported.stdout, imported.stderr, status.stdout, status.stderr):
            self.assertNotIn(data["accessToken"], output)
            self.assertNotIn(data["refreshToken"], output)

    def test_valid_token_does_not_refresh(self):
        data = self.seed()
        self.assertEqual(self.store.get()["accessToken"], data["accessToken"])
        self.assertEqual(self.backend.refreshes, 0)

    def test_expired_access_rotates_both_cookies_atomically(self):
        self.seed(ttl=-1)
        result = self.store.get()
        self.assertEqual(result["accessToken"], self.backend.new_session["accessToken"])
        saved = self.store.read()
        self.assertEqual({key: saved[key] for key in self.backend.new_session}, self.backend.new_session)
        self.assertEqual(self.backend.refreshes, 1)
        self.assertNotIn("Authorization", self.backend.cookies[0])
        self.assertFalse(list(self.directory.glob(".credentials-*")))

    def test_concurrent_processes_share_one_refresh(self):
        self.seed(ttl=-1)
        self.backend.delay = 0.2
        self.backend.access_ttl = 30
        processes = [subprocess.Popen(
            [sys.executable, "-I", str(HELPER_DIR / "credentials.py"), "token"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for _ in range(6)]
        results = []
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            results.append(json.loads(output))
        self.assertEqual(self.backend.refreshes, 1)
        self.assertTrue(all(result == results[0] for result in results))

    def test_rotated_refresh_token_is_used_next_time(self):
        original = self.seed(ttl=-1)
        self.store.get()
        data = self.store.read()
        # Expire only the access token, retaining the first rotated refresh.
        data["accessToken"] = jwt(self.backend.server, ttl=-1)
        self.store.import_session(data)
        self.store.get()
        self.assertEqual(self.backend.refreshes, 2)
        self.assertNotEqual(self.backend.cookies[1]["Cookie"], "refresh_token=" + original["refreshToken"])

    def test_rejected_refresh_preserves_store_and_redacts_response(self):
        data = self.seed(ttl=-1)
        self.backend.response_status = 403
        result = self.cli("refresh")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Sign in again", result.stderr)
        self.assertNotIn("secret-looking-body", result.stderr)
        self.assertNotIn(data["refreshToken"], result.stderr)
        self.assertEqual(self.store.read(), data)

    def test_redirect_is_not_followed(self):
        data = self.seed(ttl=-1)
        self.backend.response_status = 302
        with self.assertRaisesRegex(credentials.CredentialError, "HTTP 302"):
            self.store.get()
        self.assertEqual(self.backend.refreshes, 1)
        self.assertEqual(self.store.read(), data)

    def test_missing_cookie_preserves_store(self):
        data = self.seed(ttl=-1)
        self.backend.omit_cookie = True
        with self.assertRaisesRegex(credentials.CredentialError, "both token cookies"):
            self.store.get()
        self.assertEqual(self.store.read(), data)

    def test_refresh_cannot_switch_accounts(self):
        data = self.seed(ttl=-1)
        self.backend.response_subject = "user-two"
        with self.assertRaisesRegex(credentials.CredentialError, "different account"):
            self.store.get()
        self.assertEqual(self.store.read(), data)

    def test_expected_subject_prevents_cross_process_account_switch(self):
        self.seed(subject="user-two")
        with self.assertRaisesRegex(credentials.CredentialError, "account changed"):
            self.store.get("user-one")
        self.assertEqual(self.backend.refreshes, 0)

    def test_expired_refresh_requires_login_without_request(self):
        data = session(self.backend.server, ttl=-1, refresh_ttl=-1)
        with self.store.locked():
            self.store.write(data)
        with self.assertRaisesRegex(credentials.CredentialError, "Refresh session expired"):
            self.store.get()
        self.assertEqual(self.backend.refreshes, 0)

    def test_import_rejects_wrong_instance_pat_and_mixed_accounts(self):
        cases = [session("https://another.example"), {**session(self.backend.server), "accessToken": "pat-test"},
                 {**session(self.backend.server), "refreshToken": jwt(self.backend.server, "user-two")},
                 session(self.backend.server, refresh_ttl=-1)]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(credentials.CredentialError):
                self.store.import_session(data)
        self.assertFalse((self.directory / "credentials.json").exists())

    def test_wrong_jwt_issuer_or_audience_rejected(self):
        for extra in ({"iss": "https://another.example"}, {"aud": ["https://another.example"]}):
            data = session(self.backend.server)
            data["accessToken"] = jwt(self.backend.server, **extra)
            with self.assertRaises(credentials.CredentialError):
                self.store.import_session(data)

    def test_insecure_cloud_and_credential_urls_rejected(self):
        for server in ("http://localhost", "https://hoppscotch.io", "https://user:pass@example.com", "https://example.com/?query=1"):
            with self.subTest(server=server), self.assertRaises(credentials.CredentialError):
                credentials.Credentials(server)

    def test_symlink_and_loose_permissions_rejected(self):
        self.seed()
        path = self.directory / "credentials.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(credentials.CredentialError, "0600"):
            self.store.get()
        path.unlink()
        other = self.home / "untouched"
        other.write_text("untouched")
        path.symlink_to(other)
        with self.assertRaises(OSError):
            self.store.get()
        self.assertEqual(other.read_text(), "untouched")

    def test_lock_released_after_process_exit(self):
        self.seed()
        script = "import fcntl,os,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
        process = subprocess.Popen([sys.executable, "-I", "-c", script, str(self.directory / "credentials.lock")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        self.assertEqual(self.store.get()["subject"], "user-one")

    def test_malformed_input_does_not_expose_contents(self):
        result = self.cli("import", "secret-input-that-is-not-json")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("secret-input", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_untrusted_tls_is_rejected(self):
        self.seed(ttl=-1)
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/does/not/exist", "NODE_EXTRA_CA_CERTS": ""}):
            with self.assertRaisesRegex(credentials.CredentialError, "HTTPS refresh endpoint"):
                self.store.get()

    def test_short_lived_tokens_preserve_rotated_pair_and_are_reused(self):
        self.seed(ttl=-1)
        self.backend.access_ttl = 30
        first = self.store.get()
        self.assertEqual(self.store.get(), first)
        self.assertEqual(self.backend.refreshes, 1)
        saved = self.store.read()
        self.assertEqual(saved["refreshToken"], self.backend.new_session["refreshToken"])
        self.assertGreater(saved["refreshAfter"], time.time())
        self.assertLess(saved["refreshAfter"], credentials.claims(saved["accessToken"])["exp"])
        saved["accessToken"] = jwt(self.backend.server, ttl=-1)
        self.store.import_session(saved)
        self.store.get()
        self.assertEqual(self.backend.refreshes, 2)

    def test_expired_access_response_still_saves_usable_rotated_refresh(self):
        self.seed(ttl=-1)
        self.backend.access_ttl = -1
        with self.assertRaisesRegex(credentials.CredentialError, "Rotated credentials were saved"):
            self.store.get()
        self.assertEqual(self.store.read()["refreshToken"], self.backend.new_session["refreshToken"])
        self.backend.access_ttl = 3600
        self.store.get()
        self.assertEqual(self.backend.refreshes, 2)

    def test_node_extra_ca_alone_supports_actual_mcp_and_refresh(self):
        self.seed(ttl=-1)
        env = {**self.env}
        env.pop("SSL_CERT_FILE", None)
        with self.mcp(env=env) as call:
            result = call("tools/call", {"name": "list_user_collections", "arguments": {"type": "REST"}})
            self.assertFalse(result["result"].get("isError"), result)
        self.assertEqual(self.backend.refreshes, 1)

    def test_invalid_extra_ca_produces_redacted_configuration_error(self):
        self.seed(ttl=-1)
        with mock.patch.dict(os.environ, {"NODE_EXTRA_CA_CERTS": "/does/not/exist"}):
            with self.assertRaisesRegex(credentials.CredentialError, "HTTPS trust configuration"):
                self.store.get()

    @contextlib.contextmanager
    def mcp(self, env=None, cwd=None):
        # Exercise the real pinned MCP package, with the source or installed launcher.
        bootstrap = "const {run}=await import(process.argv[1]); await run({serverModule:process.argv[2]});"
        command = (["/usr/local/bin/hoppscotch-mcp"] if str(HELPER_DIR) == "/opt/hoppscotch-mcp/helper" else
                   [NODE, "--input-type=module", "-e", bootstrap,
                    (HELPER_DIR / "launch.mjs").as_uri(), Path(MCP).as_uri()])
        process = subprocess.Popen(command,
                                   env=env or self.env, cwd=cwd, text=True, bufsize=1,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        counter = 0
        def call(method, params=None):
            nonlocal counter
            counter += 1
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter, "method": method, "params": params or {}}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 20
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        self.fail("MCP response timed out")
                    line = process.stdout.readline()
                    if not line:
                        self.fail("MCP closed stdout: " + process.stderr.read())
                    message = json.loads(line)
                    if message.get("id") == counter:
                        return message
                self.fail("MCP response timed out")
        try:
            initialized = call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "credential-test", "version": "1.0"}})
            self.assertEqual(initialized["result"]["serverInfo"]["version"], "1.0.1")
            process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.flush()
            yield call
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_live_mcp_renews_without_restart_and_pins_account(self):
        self.seed(ttl=-1)
        with self.mcp(env={**self.env, "PATH": "/nonexistent"}) as call:
            listed = call("tools/list")
            self.assertEqual(len(listed["result"]["tools"]), 39)
            params = {"name": "list_user_collections", "arguments": {"type": "REST"}}
            first = call("tools/call", params)
            self.assertFalse(first["result"].get("isError"), first)
            self.assertEqual(self.backend.refreshes, 1)
            self.assertEqual(self.backend.calls[-1][1], "Bearer " + self.backend.new_session["accessToken"])
            data = self.store.read()
            data["accessToken"] = jwt(self.backend.server, ttl=-1)
            self.store.import_session(data)
            second = call("tools/call", params)
            self.assertFalse(second["result"].get("isError"), second)
            self.assertEqual(self.backend.refreshes, 2)
            self.assertEqual(self.backend.calls[-1][1], "Bearer " + self.backend.new_session["accessToken"])
            self.seed(subject="user-two")
            third = call("tools/call", params)
            self.assertTrue(third["result"].get("isError"), third)
            self.assertEqual(len(self.backend.calls), 2)

    def test_mcp_discovery_without_credentials_then_actionable_error(self):
        with self.mcp() as call:
            self.assertIn("tools", call("tools/list")["result"])
            result = call("tools/call", {"name": "list_user_collections", "arguments": {"type": "REST"}})
            self.assertTrue(result["result"].get("isError"), result)
            self.assertIn("hoppscotch-mcp-auth import", json.dumps(result))
        self.assertFalse(self.backend.calls)

    def test_repository_dotenv_cannot_choose_helper_home_or_tls(self):
        self.seed(ttl=-1)
        repo = self.home / "repo"
        repo.mkdir()
        (repo / ".env").write_text("SSL_CERT_FILE=/does/not/exist\nHTTPS_PROXY=http://127.0.0.1:1\nHOPPSCOTCH_SERVER_URL=https://evil.example\n")
        with self.mcp(cwd=repo) as call:
            result = call("tools/call", {"name": "list_user_collections", "arguments": {"type": "REST"}})
            self.assertFalse(result["result"].get("isError"), result)
        self.assertEqual(self.backend.refreshes, 1)

    def test_default_launcher_mode_still_discovers_tools(self):
        env = {**self.env, "HOPPSCOTCH_CREDENTIAL_HELPER": "false"}
        with self.mcp(env=env) as call:
            self.assertEqual(len(call("tools/list")["result"]["tools"]), 39)
        self.assertEqual(self.backend.refreshes, 0)

    def test_fetch_hook_only_supplies_credentials_to_expected_endpoint(self):
        script = """
          import assert from 'node:assert/strict';
          const {installCredentialFetch}=await import(process.argv[1]);
          let requests=0;
          globalThis.fetch=async()=>{requests++;return new Response('ok')};
          installCredentialFetch(process.env.HOPPSCOTCH_SERVER_URL);
          await fetch('https://unrelated.example');
          assert.equal(requests,1);
          await assert.rejects(fetch('https://unrelated.example', {headers:{Authorization:'Bearer hoppscotch-credential-helper'}}), /unexpected endpoint/);
          assert.equal(requests,1);
        """
        result = subprocess.run([NODE, "--input-type=module", "-e", script, (HELPER_DIR / "launch.mjs").as_uri()],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_environment_is_captured_before_upstream_code(self):
        self.seed(ttl=-1)
        script = """
          import assert from 'node:assert/strict';
          const {installCredentialFetch}=await import(process.argv[1]);
          let authorization;
          globalThis.fetch=async(input,init)=>{authorization=init.headers.get('authorization');return new Response('ok')};
          installCredentialFetch(process.env.HOPPSCOTCH_SERVER_URL);
          process.env.HOME='/does/not/exist';
          process.env.SSL_CERT_FILE='/does/not/exist';
          await fetch(process.env.HOPPSCOTCH_SERVER_URL+'/backend/graphql', {headers:{Authorization:'Bearer hoppscotch-credential-helper'}});
          assert.ok(authorization.startsWith('Bearer ey'));
        """
        result = subprocess.run([NODE, "--input-type=module", "-e", script, (HELPER_DIR / "launch.mjs").as_uri()],
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backend.refreshes, 1)

    def test_static_token_and_helper_configuration_conflict(self):
        bootstrap = "const {run}=await import(process.argv[1]); await run();"
        result = subprocess.run([NODE, "--input-type=module", "-e", bootstrap, (HELPER_DIR / "launch.mjs").as_uri()],
                                env={**self.env, "HOPPSCOTCH_ACCESS_TOKEN": "sensitive-static-token"},
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unset HOPPSCOTCH_ACCESS_TOKEN", result.stderr)
        self.assertNotIn("sensitive-static-token", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
