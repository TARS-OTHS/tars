/**
 * anthropic-client.js — Anthropic API client for TARS services.
 *
 * Reads Claude auth token from TARS secrets (NOT from OpenClaw internals).
 * Token is imported once during setup and auto-refreshed by a cron job
 * that watches OC's auth-profiles.json for changes.
 *
 * On auth failure: logs loud error with actionable fix, posts to ops-alerts.
 */

const https = require('https');
const fs = require('fs');
const path = require('path');

// Token lives in our secrets dir, not OC's internals
const SECRETS_DIR = process.env.SECRETS_DIR || '/app/secrets';
const TOKEN_PATH = path.join(SECRETS_DIR, 'claude-token');
const OPS_ALERTS_URL = process.env.OPS_ALERTS_URL || null;

const ANTHROPIC_API_HOST = 'api.anthropic.com';
const ANTHROPIC_API_VERSION = '2023-06-01';
const DEFAULT_MODEL = process.env.CLAUDE_MODEL || 'claude-sonnet-4-20250514';
const DEFAULT_MAX_TOKENS = 4096;

/**
 * Read Claude token from TARS secrets.
 * Reads fresh from disk each call to pick up cron-refreshed tokens.
 */
function getToken() {
    if (!fs.existsSync(TOKEN_PATH)) {
        const msg = `Claude token not found at ${TOKEN_PATH}\n` +
            '  Fix: run "tars reauth" or re-run "openclaw setup" then "tars import-token"';
        console.error(`[anthropic-client] ERROR: ${msg}`);
        postAlert(`Claude auth token missing — services cannot reach Claude.\n${msg}`);
        throw new Error(msg);
    }

    const token = fs.readFileSync(TOKEN_PATH, 'utf8').trim();
    if (!token) {
        const msg = 'Claude token file is empty — run "tars reauth" to fix';
        console.error(`[anthropic-client] ERROR: ${msg}`);
        postAlert(msg);
        throw new Error(msg);
    }

    return token;
}

/**
 * Detect token type: OAuth (Claude Max subscription) or API key.
 */
function isOAuthToken(token) {
    return token.startsWith('sk-ant-oat');
}

function isApiKey(token) {
    return token.startsWith('sk-ant-api');
}

/**
 * Post an alert to the ops-alerts channel (Discord/Slack).
 * Fire-and-forget — never throws.
 */
function postAlert(message) {
    if (!OPS_ALERTS_URL) return;

    const payload = JSON.stringify({
        content: `**TARS Auth Alert**\n${message}`
    });

    try {
        const url = new URL(OPS_ALERTS_URL);
        const req = https.request({
            hostname: url.hostname,
            port: url.port || 443,
            path: url.pathname,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload),
            },
        });
        req.on('error', () => {}); // swallow — alerting should never break the caller
        req.write(payload);
        req.end();
    } catch (_) {
        // alerting must never throw
    }
}

/**
 * Call the Anthropic Messages API.
 *
 * Detects OAuth vs API key and uses correct auth method.
 * On 401: posts alert with actionable fix instructions.
 */
function callAnthropic(prompt, options = {}) {
    return new Promise((resolve, reject) => {
        let token;
        try {
            token = getToken();
        } catch (err) {
            return reject(err);
        }

        const model = options.model || DEFAULT_MODEL;
        const maxTokens = options.maxTokens || DEFAULT_MAX_TOKENS;
        const timeoutMs = options.timeoutMs || 120000;
        const isOAuth = isOAuthToken(token);

        const body = JSON.stringify({
            model,
            max_tokens: maxTokens,
            messages: [{ role: 'user', content: prompt }],
            ...(options.system ? { system: options.system } : {}),
        });

        const headers = {
            'Content-Type': 'application/json',
            'anthropic-version': ANTHROPIC_API_VERSION,
            'accept': 'application/json',
        };

        if (isOAuth) {
            headers['Authorization'] = `Bearer ${token}`;
            headers['anthropic-beta'] = 'oauth-2025-04-20';
            headers['user-agent'] = 'tars-services/1.0';
        } else {
            headers['x-api-key'] = token;
        }

        const req = https.request({
            hostname: ANTHROPIC_API_HOST,
            path: '/v1/messages',
            method: 'POST',
            headers,
            timeout: timeoutMs,
        }, (res) => {
            let data = '';
            res.on('data', (chunk) => { data += chunk; });
            res.on('end', () => {
                if (res.statusCode === 401 || res.statusCode === 403) {
                    const msg = `Claude auth failed (HTTP ${res.statusCode}) — ` +
                        (isOAuth
                            ? 'OAuth token expired. Run "tars reauth" or "openclaw setup" to re-authenticate.'
                            : 'API key rejected. Check your key or run "tars reauth".');
                    console.error(`[anthropic-client] ${msg}`);
                    postAlert(msg);
                    return reject(new Error(msg));
                }
                if (res.statusCode === 429) {
                    const msg = 'Claude rate limited (429) — too many requests. Will retry automatically.';
                    console.warn(`[anthropic-client] ${msg}`);
                    return reject(new Error(msg));
                }
                if (res.statusCode !== 200) {
                    const msg = `Anthropic API error ${res.statusCode}: ${data.slice(0, 500)}`;
                    console.error(`[anthropic-client] ${msg}`);
                    return reject(new Error(msg));
                }
                try {
                    const parsed = JSON.parse(data);
                    const text = (parsed.content || [])
                        .filter(b => b.type === 'text')
                        .map(b => b.text)
                        .join('');
                    resolve(text);
                } catch (err) {
                    reject(new Error(`Failed to parse Anthropic response: ${err.message}`));
                }
            });
        });

        req.on('timeout', () => {
            req.destroy();
            reject(new Error(`Anthropic API timeout after ${timeoutMs}ms`));
        });

        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

/**
 * Synchronous wrapper — calls Anthropic API using a child process.
 */
function callAnthropicSync(prompt, options = {}) {
    const { spawnSync } = require('child_process');
    const optionsB64 = Buffer.from(JSON.stringify(options)).toString('base64');
    const script = `
        const { callAnthropic } = require(${JSON.stringify(__filename)});
        let chunks = [];
        process.stdin.on('data', c => chunks.push(c));
        process.stdin.on('end', () => {
            const prompt = Buffer.concat(chunks).toString('utf8');
            const options = JSON.parse(Buffer.from(process.argv[1], 'base64').toString('utf8'));
            callAnthropic(prompt, options)
                .then(r => { process.stdout.write(r); })
                .catch(e => { process.stderr.write(e.message); process.exit(1); });
        });
    `;

    const result = spawnSync(process.execPath, ['-e', script, optionsB64], {
        input: prompt,
        encoding: 'utf8',
        timeout: options.timeoutMs || 120000,
        maxBuffer: 2 * 1024 * 1024,
        env: { ...process.env },
    });
    if (result.error) throw result.error;
    if (result.status !== 0) {
        throw new Error(result.stderr || 'callAnthropicSync failed');
    }
    return result.stdout;
}

module.exports = { callAnthropic, callAnthropicSync, getToken };
