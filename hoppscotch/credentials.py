#!/usr/bin/python3
"""Private, process-locked credentials for the self-hosted Hoppscotch MCP launcher."""

import argparse
import base64
import contextlib
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_BYTES = 65536
REFRESH_WINDOW = 120
LOCK_TIMEOUT = 20


class CredentialError(Exception):
    pass


def instance_urls(value):
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or url.hostname.lower() in {"hoppscotch.io", "www.hoppscotch.io"}
                or any(c.isspace() for c in value)):
            raise ValueError()
        port = url.port
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        authority = host + (f":{port}" if port and port != 443 else "")
        server = urllib.parse.urlunsplit(("https", authority, url.path.rstrip("/"), "", ""))
        return server, server + "/backend"
    except (TypeError, ValueError):
        raise CredentialError("Set HOPPSCOTCH_SERVER_URL to your self-hosted HTTPS frontend URL.") from None


def claims(token):
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError()
        parts = token.split(".")
        if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if (not isinstance(payload, dict) or not isinstance(payload.get("sub"), str)
                or not payload["sub"] or type(payload.get("exp")) not in (int, float)
                or not math.isfinite(payload["exp"])):
            raise ValueError()
        return payload
    except (ValueError, TypeError, UnicodeError):
        raise CredentialError("Expected session JWTs with subject and expiry claims; PATs are unsupported.") from None


def validate_session(data, server):
    _, api_url = instance_urls(server)
    if (not isinstance(data, dict) or data.get("apiUrl") != api_url
            or data.get("apiType") != "selfhost"):
        raise CredentialError("Session belongs to another instance or is not a self-hosted session.")
    access = claims(data.get("accessToken"))
    refresh = claims(data.get("refreshToken"))
    if access["sub"] != refresh["sub"]:
        raise CredentialError("Access and refresh tokens belong to different accounts.")
    for payload in (access, refresh):
        # Claim checks bind the imported session to the configured instance;
        # signatures are verified by Hoppscotch, not by this local helper.
        issuer, _ = instance_urls(payload.get("iss"))
        audience = payload.get("aud")
        if isinstance(audience, str):
            audience = [audience]
        if issuer != server or not isinstance(audience, list) or not any(
            isinstance(item, str) and item.rstrip("/") == server for item in audience
        ):
            raise CredentialError("JWT issuer or audience does not match the configured instance.")
    return access, refresh


def read_json(stream):
    try:
        raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise CredentialError("Expected a session JSON object of at most 64 KiB.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def refresh_pair(api_url, refresh_token):
    request = urllib.request.Request(
        api_url + "/v1/auth/refresh",
        headers={"Cookie": "refresh_token=" + refresh_token, "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise CredentialError("Refresh endpoint returned an unexpected status.")
            tokens = {}
            for header in response.headers.get_all("Set-Cookie", []):
                name, separator, value = header.split(";", 1)[0].partition("=")
                if separator and name.strip() in {"access_token", "refresh_token"}:
                    name = name.strip()
                    if name in tokens:
                        raise CredentialError("Refresh response contains duplicate token cookies.")
                    tokens[name] = urllib.parse.unquote(value)
            if set(tokens) != {"access_token", "refresh_token"}:
                raise CredentialError("Refresh response did not contain both token cookies.")
            return tokens["access_token"], tokens["refresh_token"]
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403, 404):
            raise CredentialError(
                f"Refresh rejected (HTTP {code}). Sign in again and import the new session."
            ) from None
        raise CredentialError(f"Refresh failed (HTTP {code}); no credentials were replaced.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CredentialError("Cannot reach the HTTPS refresh endpoint; no credentials were replaced.") from None


class Credentials:
    def __init__(self, server, directory=None):
        self.server, self.api_url = instance_urls(server)
        self.directory = Path(directory) if directory else Path.home() / ".config/hoppscotch-mcp-helper"

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CredentialError("Credential directory must be a directory owned by the current user.")
        self.directory.chmod(0o700)
        lock = os.open(self.directory / "credentials.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise CredentialError("Invalid credential lock file.")
            os.fchmod(lock, 0o600)
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CredentialError("Timed out waiting for another credential operation.") from None
                    time.sleep(0.05)
            yield
        finally:
            # Do not unlink: every process must continue locking the same inode.
            os.close(lock)

    def read(self):
        try:
            fd = os.open(self.directory / "credentials.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise CredentialError("No helper session. Sign in once and run hoppscotch-mcp-auth import.") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise CredentialError("Credential file must be owned by the current user with mode 0600.")
            return read_json(stream)

    def write(self, session):
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(session, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / "credentials.json")
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def import_session(self, data):
        access, refresh = validate_session(data, self.server)
        if refresh["exp"] <= time.time():
            raise CredentialError("Refresh session expired. Sign in again before importing.")
        session = {
            "apiUrl": self.api_url, "apiType": "selfhost",
            "accessToken": data["accessToken"], "refreshToken": data["refreshToken"],
        }
        with self.locked():
            self.write(session)
        return access, refresh

    def get(self, expected_subject=None):
        with self.locked():
            session = self.read()
            access, refresh = validate_session(session, self.server)
            if expected_subject is not None and access["sub"] != expected_subject:
                raise CredentialError("Session account changed. Restart the MCP client to use the new account.")
            if access["exp"] <= time.time() + REFRESH_WINDOW:
                if refresh["exp"] <= time.time():
                    raise CredentialError("Refresh session expired. Sign in again and import the new session.")
                new_access, new_refresh = refresh_pair(self.api_url, session["refreshToken"])
                updated = {**session, "accessToken": new_access, "refreshToken": new_refresh}
                new_access_claims, new_refresh_claims = validate_session(updated, self.server)
                if new_access_claims["sub"] != access["sub"]:
                    raise CredentialError("Refresh returned a different account; credentials were not replaced.")
                if (new_access_claims["exp"] <= time.time() + REFRESH_WINDOW
                        or new_refresh_claims["exp"] <= time.time()):
                    raise CredentialError("Refresh returned tokens with insufficient remaining validity.")
                self.write(updated)
                session, access = updated, new_access_claims
            return {"accessToken": session["accessToken"], "subject": access["sub"]}

    def status(self):
        with self.locked():
            access, refresh = validate_session(self.read(), self.server)
            def timestamp(value):
                return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
            return {
                "server": self.server,
                "accessExpiresAt": timestamp(access["exp"]),
                "refreshExpiresAt": timestamp(refresh["exp"]),
                "refreshExpired": refresh["exp"] <= time.time(),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("import", "status", "refresh", "token"),
                        help="token is the launcher's private stdout protocol; use status for diagnostics")
    parser.add_argument("--server-url", default=os.environ.get("HOPPSCOTCH_SERVER_URL"))
    parser.add_argument("--subject", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        credentials = Credentials(args.server_url)
        if args.command == "import":
            credentials.import_session(read_json(sys.stdin.buffer))
            print("Session imported. The helper will renew access tokens when needed.")
        elif args.command == "token":
            print(json.dumps(credentials.get(args.subject)))
        else:
            if args.command == "refresh":
                credentials.get()
            print(json.dumps(credentials.status(), indent=2))
    except (CredentialError, OSError, OverflowError) as error:
        message = str(error) if isinstance(error, CredentialError) else "Cannot access the private credential store."
        print("Hoppscotch credentials: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
