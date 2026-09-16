# Hoppscotch MCP in Coder workspaces

The base image installs `@hoppscotch/mcp-server` at the version pinned by
`HOPPSCOTCH_MCP_VERSION` in `Dockerfile.base`. The executable is
`/usr/local/bin/hoppscotch-mcp`, its dependencies live under `/opt/hoppscotch-mcp`,
and its pinned Node.js interpreter is `/opt/hoppscotch-mcp/bin/node`. The launcher
uses that interpreter directly, so an existing home volume or a project's Node
version selection cannot hide or replace it. Playwright and webdev images
inherit it.
It runs on demand as a stdio subprocess of each MCP client; no service or exposed
MCP port is needed.

The image contains no instance URL, client registrations, or credentials. Those
belong to the Coder template and workspace. In the `dev` workspace, `/home/coder`
is a persistent Docker volume, so image-layer configuration there would be
hidden by the mounted home.

## Changes to the `Codex` template

These are integration instructions for the template's source repository; this
repository only builds images.

1. Select a rebuilt base, Playwright, or webdev image containing the MCP server.
   Merging image changes publishes development tags through the existing build
   workflow. Stable tags change only through the separate promotion workflow.
2. Add workspace initialization that merges the registrations below into the
   runtime user's configuration after the home volume is mounted and after any
   existing Codex/Claude setup or dotfiles step that writes those files.
3. Ensure the initialization is repeatable: preserve unrelated settings and MCP
   servers, update only the `hoppscotch` entry, and avoid duplicate TOML tables.
   Honor `CODEX_HOME` if the template sets it; otherwise Codex uses
   `/home/coder/.codex/config.toml`.
4. Enable the credential helper as shown below. Import an initial session once
   using the setup procedure in this guide. Keep the helper's private directory
   in the persistent home volume; template startup must not overwrite it with an
   older token pair. Credentials belong in this runtime store, never Docker build
   arguments, tracked Terraform values, or client configuration.
5. Apply the updated template to `dev` and restart the workspace. Restart any
   already-running MCP clients after registration changes or an intentional
   account switch. Routine token renewal does not require a client restart.

Coder supports a lifecycle script with this shape (adapt the agent name and
script path to the template):

```hcl
resource "coder_script" "hoppscotch_mcp" {
  agent_id           = coder_agent.main.id
  display_name       = "Configure Hoppscotch MCP"
  script             = file("${path.module}/scripts/configure-hoppscotch-mcp.sh")
  run_on_start       = true
  start_blocks_login = true
}
```

`configure-hoppscotch-mcp.sh` is a template-side script to implement using the
template's existing configuration tooling. It should check that
`/usr/local/bin/hoppscotch-mcp` and `/opt/hoppscotch-mcp/bin/node` are executable,
then perform the merges below. Coordinate it with the existing client setup: separate
startup scripts can run concurrently, and `start_blocks_login` alone does not
order them or defer a client that another script starts. Incorporating the merge
into the existing setup script before it starts Codex is also suitable.

### Codex registration

Merge the following into the Codex user configuration:

```toml
[mcp_servers.hoppscotch]
command = "/usr/local/bin/hoppscotch-mcp"
env_vars = ["HOPPSCOTCH_DEFAULT_TEAM_ID"]

[mcp_servers.hoppscotch.env]
HOPPSCOTCH_SERVER_URL = "https://hoppscotch.treyturner.info"
HOPPSCOTCH_CREDENTIAL_HELPER = "true"
HOPPSCOTCH_TOOL_PROFILE = "core"
HOPPSCOTCH_STRICT_ENV = "true"
```

The default team ID is optional. Remove any existing `HOPPSCOTCH_ACCESS_TOKEN`
entry or forwarding rule when enabling the helper: the two authentication modes
are mutually exclusive. Strict environment mode keeps repository `.env` files
from supplying authentication or changing trust-sensitive settings; helper mode
always enables it.

### Optional Claude Code registration

After Claude Code is installed, register the same executable at user scope:

```bash
claude mcp add \
  --env HOPPSCOTCH_SERVER_URL=https://hoppscotch.treyturner.info \
  --env HOPPSCOTCH_CREDENTIAL_HELPER=true \
  --env HOPPSCOTCH_TOOL_PROFILE=core \
  --env HOPPSCOTCH_STRICT_ENV=true \
  --scope user --transport stdio hoppscotch \
  -- /usr/local/bin/hoppscotch-mcp
```

Claude and Codex share the helper store for the same OS user. The initialization
script must handle an existing user-scoped `hoppscotch` registration, updating only that
entry rather than repeatedly adding it or replacing the entire user config.
Skip this step when Claude is not installed. This configures Claude Code running
inside the workspace.

## Authentication and instance requirements

The server derives the self-hosted API endpoint as
`https://hoppscotch.treyturner.info/backend/graphql`. It authenticates using a
Hoppscotch session JWT. Personal access tokens beginning with `pat-` are REST-only
and do not work with this MCP server.

### One-time session import

1. On a machine with a browser, configure the upstream Hoppscotch MCP server for
   the same instance and call an authenticated tool to complete device login.
   The upstream server writes `~/.config/hoppscotch-mcp/auth.json` containing both
   `accessToken` and `refreshToken`. Both are required; an access token alone
   cannot renew a session.
2. From that machine, transfer the complete session directly over Coder SSH:

   ```bash
   coder ssh dev -- /usr/local/bin/hoppscotch-mcp-auth import \
     --server-url https://hoppscotch.treyturner.info \
     < "$HOME/.config/hoppscotch-mcp/auth.json"
   ```

   If the session file already exists inside the workspace, use the local
   `hoppscotch-mcp-auth import --server-url ... < path/to/auth.json` equivalent.
   Import validates the instance and token claims and stores only the required
   fields. It does not print tokens. JWT signatures are verified by Hoppscotch
   when used, not by this local importer.
3. Inside `dev`, check the session without exposing credentials:

   ```bash
   hoppscotch-mcp-auth status --server-url https://hoppscotch.treyturner.info
   hoppscotch-mcp-auth refresh --server-url https://hoppscotch.treyturner.info
   ```

   `status` reports expiries without a network request. `refresh` ensures a valid
   access token, renewing only when it is near expiry, and reports expiries.
   Neither prints tokens. The `token` subcommand is the launcher's private stdout
   protocol and should not be used for diagnostics or agent tool calls.

The private store is
`/home/coder/.config/hoppscotch-mcp-helper/credentials.json` (directory mode 0700,
file mode 0600). Import replaces this helper session intentionally; a running MCP
process refuses to switch to a different account until restarted. Import a fresh
session again after expiry or revocation. In helper mode use this import flow
instead of the upstream MCP `reauth` tool.

### Automatic renewal

Version 1.0.1 uses an incompatible unversioned, bearer-token refresh request for
Community Edition. Helper mode runs the unmodified MCP package with an internal
placeholder and supplies the real access token only to its configured GraphQL
endpoint. Before each API request, the launcher asks the helper for a token.
The helper schedules renewal before access-token expiry, reserving 10% of the
observed remaining lifetime up to a maximum of two minutes. This also supports
short-lived access tokens without refreshing on every request. When due, it calls
`/backend/v1/auth/refresh` with the `refresh_token` cookie, accepts the rotated
`access_token` and `refresh_token` cookies, and atomically replaces the store.
Requests use verified HTTPS and refuse redirects. A private CA configured through
`NODE_EXTRA_CA_CERTS` is also loaded by the Python refresh client; a duplicate
`SSL_CERT_FILE` setting is not required. Forward `NODE_EXTRA_CA_CERTS` into the
MCP client's environment when needed. The helper never logs token
values or backend response bodies, and it snapshots its environment before the
upstream server loads repository `.env` files.

A process lock serializes refresh and import across local clients. One client
refreshes; the others reread the new token pair. Network failures, rejected
requests, and malformed or wrong-account responses leave the previous file
intact and report an error. A well-formed pair with a usable rotated refresh token
is saved even if its access token is already expired, allowing a later renewal
attempt to recover. No authenticated GraphQL request is
sent by the helper when renewal fails. The helper does not replay API requests.
Cloud and ordinary explicit-token configurations retain upstream behavior when
`HOPPSCOTCH_CREDENTIAL_HELPER` is unset or false.

Renewal is on demand, so a workspace idle beyond refresh-token expiry still needs
a new login. Community Edition defaults to one-day access tokens and seven-day
refresh tokens, configurable by the backend. Its backend stores one refresh-token
hash per account: signing in elsewhere or using the original browser MCP session
to refresh can invalidate the helper's pair. Use the imported session from this
workspace going forward. Separate workspaces do not share the local lock; a
dedicated Hoppscotch account avoids competing sessions. Enterprise/custom backend
compatibility requires separate verification.

If agents need to execute requests against workspace-local/private APIs, opt in
to `HOPPSCOTCH_ALLOW_PRIVATE_HOSTS=true` in their MCP environment. This changes
request-execution protection and is not required merely to select a self-hosted
Hoppscotch instance.

## Rollout verification

- Check `command -v hoppscotch-mcp` and
  `/opt/hoppscotch-mcp/bin/node --version` inside the updated workspace,
  including with its existing home volume.
- Check the registration using `codex mcp get hoppscotch` and, if installed,
  `claude mcp get hoppscotch`.
- After importing a session, use a read-only tool such as `list_user_collections`
  to verify authenticated connectivity. Tool discovery alone does not verify
  credentials or backend compatibility.
- Restart the workspace and verify the registrations still exist without
  duplicates and that unrelated MCP entries remain intact.

## Development checks

`python3 -I tests/hoppscotch/test_credentials.py` runs the helper tests and the
actual installed MCP package against a local HTTPS fixture. It requires the
image's pinned Node/MCP installation and OpenSSL. CI runs the same suite inside
the newly built image with external networking disabled, before building derived
images or publishing the base. The fixture uses generated test JWTs and a
temporary trusted CA; no real Hoppscotch credentials are used.

References:

- [Hoppscotch MCP configuration and authentication](https://github.com/hoppscotch/hoppscotch-mcp-server)
- [Community Edition refresh endpoint](https://github.com/hoppscotch/hoppscotch/blob/main/packages/hoppscotch-backend/src/auth/auth.controller.ts)
- [Refresh-token rotation and account storage](https://github.com/hoppscotch/hoppscotch/blob/main/packages/hoppscotch-backend/src/auth/auth.service.ts)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp/)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Coder lifecycle scripts](https://coder.com/docs/admin/templates/extending-templates)
