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
4. Supply the JWT through the template's runtime secret mechanism to the process
   launching Codex or Claude. A variable exported only inside an initialization
   script does not configure a separately launched client. Do not put the JWT in
   Docker build arguments, tracked Terraform values, or a `codex mcp add --env`
   argument, which would save its value in the client configuration.
5. Apply the updated template to `dev` and restart the workspace. Restart any
   already-running MCP clients after registration or credential changes.

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
env_vars = ["HOPPSCOTCH_ACCESS_TOKEN", "HOPPSCOTCH_DEFAULT_TEAM_ID"]

[mcp_servers.hoppscotch.env]
HOPPSCOTCH_SERVER_URL = "https://hoppscotch.treyturner.info"
HOPPSCOTCH_TOOL_PROFILE = "core"
HOPPSCOTCH_STRICT_ENV = "true"
```

`env_vars` forwards values from the environment of the Codex process without
storing the token in TOML. A default team ID is optional. Strict environment mode
keeps repository `.env` files from supplying authentication or changing
trust-sensitive settings.

### Optional Claude Code registration

After Claude Code is installed, register the same executable at user scope:

```bash
claude mcp add \
  --env HOPPSCOTCH_SERVER_URL=https://hoppscotch.treyturner.info \
  --env HOPPSCOTCH_TOOL_PROFILE=core \
  --env HOPPSCOTCH_STRICT_ENV=true \
  --scope user --transport stdio hoppscotch \
  -- /usr/local/bin/hoppscotch-mcp
```

Launch Claude with the same runtime JWT environment. The initialization script
must handle an existing user-scoped `hoppscotch` registration, updating only that
entry rather than repeatedly adding it or replacing the entire user config.
Skip this step when Claude is not installed. This configures Claude Code running
inside the workspace.

## Authentication and instance requirements

The server derives the self-hosted API endpoint as
`https://hoppscotch.treyturner.info/backend/graphql`. It authenticates using a
Hoppscotch session JWT. Personal access tokens beginning with `pat-` are REST-only
and do not work with this MCP server.

For a headless Coder workspace, obtain a JWT by completing Hoppscotch MCP device
login on a machine with a browser, then provision its `accessToken` as the runtime
`HOPPSCOTCH_ACCESS_TOKEN` secret. Treat it as an expiring session: replace the
secret and restart the clients when it expires. An explicitly supplied token
takes precedence over the stored browser session, and the MCP `reauth` tool
cannot replace it.

Alternatively, browser login stores a session at
`/home/coder/.config/hoppscotch-mcp/auth.json`. The persistent home retains this
file across workspace restarts. Container login requires a browser with access
to the server's random localhost callback port, generally through a local
forward; the Coder browser proxy alone is not that callback. Headless detection
also needs `HOPPSCOTCH_FORCE_BROWSER_LOGIN=true` when deliberately using this
flow.

Version 1.0.1 does not successfully refresh current Community Edition sessions:
its refresh endpoint is cookie-based and this release does not use that flow.
Community Edition defaults to a one-day JWT lifetime, configurable by the
backend. Persisting `auth.json` does not extend that lifetime. Enterprise/custom
backend refresh behavior is unverified upstream. Unattended, long-lived access
therefore requires a token renewal mechanism or compatible refresh support.

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
- With a valid JWT supplied, use a read-only tool such as `list_user_collections`
  to verify authenticated connectivity. Tool discovery alone does not verify
  credentials or backend compatibility.
- Restart the workspace and verify the registrations still exist without
  duplicates and that unrelated MCP entries remain intact.

References:

- [Hoppscotch MCP configuration and authentication](https://github.com/hoppscotch/hoppscotch-mcp-server)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp/)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Coder lifecycle scripts](https://coder.com/docs/admin/templates/extending-templates)
