import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath, pathToFileURL } from 'node:url';

const execFileAsync = promisify(execFile);
const helper = fileURLToPath(new URL('./credentials.py', import.meta.url));
const managedToken = 'hoppscotch-credential-helper';

export function installCredentialFetch(serverUrl) {
  let url;
  try {
    url = new URL(serverUrl);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
        || ['hoppscotch.io', 'www.hoppscotch.io'].includes(url.hostname)
        || /\s/.test(serverUrl)) throw new Error();
  } catch {
    throw new Error('The credential helper requires an explicit self-hosted HTTPS HOPPSCOTCH_SERVER_URL.');
  }
  url.pathname = url.pathname.replace(/\/+$/, '') || '/';
  const server = url.href.replace(/\/$/, '');
  const endpoint = server + '/backend/graphql';
  const originalFetch = globalThis.fetch;
  // Snapshot before upstream dotenv runs. A repository .env must not choose
  // the helper's TLS trust, proxy, HOME, or Python environment.
  const helperEnv = { ...process.env };
  let subject;

  globalThis.fetch = async (input, init) => {
    const target = input instanceof Request ? input.url : String(input);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    if (headers.get('authorization') !== `Bearer ${managedToken}`) {
      return originalFetch(input, init);
    }
    if (target !== endpoint) {
      throw new Error('Refusing to supply Hoppscotch credentials to an unexpected endpoint.');
    }
    let result;
    try {
      const args = ['-I', helper, 'token', '--server-url', server];
      if (subject !== undefined) args.push('--subject', subject);
      const { stdout } = await execFileAsync('/usr/bin/python3', args, {
        env: helperEnv, maxBuffer: 65536, timeout: 35000,
      });
      result = JSON.parse(stdout);
    } catch (error) {
      // The Python helper emits only fixed diagnostic messages, never tokens or
      // HTTP response bodies. Never forward execFile's stdout/error object.
      const diagnostic = typeof error.stderr === 'string' && error.stderr.startsWith('Hoppscotch credentials: ')
        ? error.stderr.trim() : 'Hoppscotch credential helper failed. Run hoppscotch-mcp-auth status.';
      throw new Error(diagnostic);
    }
    if (typeof result.accessToken !== 'string' || typeof result.subject !== 'string') {
      throw new Error('Invalid credential helper response.');
    }
    if (subject !== undefined && subject !== result.subject) {
      throw new Error('Session account changed. Restart the MCP client to use the new account.');
    }
    subject = result.subject;
    headers.set('authorization', `Bearer ${result.accessToken}`);
    // Do not send a bearer credential through a redirect or replay a mutation.
    return originalFetch(input, { ...init, headers, redirect: 'error' });
  };
}

export async function run({ serverModule = new URL('../node_modules/@hoppscotch/mcp-server/dist/index.js', import.meta.url) } = {}) {
  if (process.env.HOPPSCOTCH_CREDENTIAL_HELPER === 'true') {
    if (process.env.HOPPSCOTCH_ACCESS_TOKEN) {
      throw new Error('Unset HOPPSCOTCH_ACCESS_TOKEN when enabling the credential helper.');
    }
    installCredentialFetch(process.env.HOPPSCOTCH_SERVER_URL);
    // Use the upstream explicit-token path; it must not open a browser or manage
    // this helper's separate credential file. Real tokens never enter the env.
    process.env.HOPPSCOTCH_ACCESS_TOKEN = managedToken;
    process.env.HOPPSCOTCH_STRICT_ENV = 'true';
  }
  await import(serverModule);
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  run().catch(() => {
    console.error('Cannot start Hoppscotch MCP: check the self-hosted URL and credential-helper configuration; unset HOPPSCOTCH_ACCESS_TOKEN in helper mode.');
    process.exitCode = 1;
  });
}
