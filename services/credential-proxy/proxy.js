#!/usr/bin/env node
/**
 * OpenClaw Credential Proxy
 * 
 * Runs on the HOST. Intercepts HTTP(S) requests from containerized agents
 * and injects real API keys in flight. Agents never see the actual keys.
 * 
 * Routes credentials by source subnet:
 *   172.17.0.0/16 → OC agents (proxy-config-oc.json)
 *   172.18.0.0/16 → CC agents (proxy-config-cc.json)
 * 
 * Usage: node proxy.js [--config-dir /path/to/configs]
 */

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const url = require('url');
const net = require('net');
const { execSync } = require('child_process');

// --- Config ---
const args = process.argv.slice(2);
const configDirIdx = args.indexOf('--config-dir');
const CONFIG_DIR = configDirIdx >= 0 ? args[configDirIdx + 1] : __dirname;

// Load per-container configs
const configs = {
  oc: JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'proxy-config-oc.json'), 'utf8')),
  cc: JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'proxy-config-cc.json'), 'utf8')),
};

// Shared settings from OC config (or could be a separate shared config)
const PORT = configs.oc.listenPort || 8899;
const VAULT_PATH = configs.oc.vaultPath || path.join(process.env.HOME, '.secrets-vault/secrets.age');
const AGE_KEY_PATH = configs.oc.ageKeyPath || path.join(process.env.HOME, '.config/age/key.txt');
const LOG_FILE = configs.oc.logFile || process.env.PROXY_LOG_PATH || '/app/logs/proxy.log';

// Subnet routing
const SUBNET_MAP = [
  { cidr: '172.17.0.0/16', configKey: 'oc', label: 'OC' },
  { cidr: '172.18.0.0/16', configKey: 'cc', label: 'CC' },
];

// --- Subnet matching ---
function ipToLong(ip) {
  return ip.split('.').reduce((acc, octet) => (acc << 8) + parseInt(octet), 0) >>> 0;
}

function cidrContains(cidr, ip) {
  const [network, bits] = cidr.split('/');
  const mask = (~0 << (32 - parseInt(bits))) >>> 0;
  return (ipToLong(ip) & mask) === (ipToLong(network) & mask);
}

function getConfigForSource(sourceIp) {
  for (const entry of SUBNET_MAP) {
    if (cidrContains(entry.cidr, sourceIp)) {
      return { config: configs[entry.configKey], label: entry.label };
    }
  }
  // Default to OC config for localhost/unknown
  return { config: configs.oc, label: 'DEFAULT' };
}

// --- Decrypt Vault at Startup ---
let secrets = {};

function loadVault() {
  const fs = require('fs');
  if (!fs.existsSync(VAULT_PATH)) {
    log(`WARNING: Vault not found at ${VAULT_PATH} — starting with empty secrets. Run setup.sh to configure.`);
    secrets = {};
    return;
  }
  if (!fs.existsSync(AGE_KEY_PATH)) {
    log(`WARNING: Age key not found at ${AGE_KEY_PATH} — starting with empty secrets.`);
    secrets = {};
    return;
  }
  try {
    const raw = execSync(`age -d -i "${AGE_KEY_PATH}" "${VAULT_PATH}"`, { encoding: 'utf8' });
    secrets = JSON.parse(raw);
    log(`Vault loaded: ${Object.keys(secrets).length} secrets`);
  } catch (e) {
    log(`WARNING: Failed to decrypt vault: ${e.message} — starting with empty secrets.`);
    secrets = {};
  }
}

function getSecret(secretKey) {
  return secrets[secretKey] || null;
}

// Reload vault + configs on SIGHUP
process.on('SIGHUP', () => {
  log('Reloading vault and configs...');
  loadVault();
  try {
    configs.oc = JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'proxy-config-oc.json'), 'utf8'));
    configs.cc = JSON.parse(fs.readFileSync(path.join(CONFIG_DIR, 'proxy-config-cc.json'), 'utf8'));
    log('Configs reloaded');
  } catch (e) {
    log(`WARNING: Failed to reload configs: ${e.message}`);
  }
});

// --- Logging ---
function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  process.stdout.write(line);
  try {
    fs.appendFileSync(LOG_FILE, line);
  } catch (e) { /* ignore */ }
}

// --- Leak Detection ---
function checkForLeaks(data) {
  if (!data) return false;
  const str = typeof data === 'string' ? data : data.toString('utf8');
  for (const [key, secret] of Object.entries(secrets)) {
    if (typeof secret === 'string' && secret.length > 12 && str.includes(secret)) {
      log(`⚠️  LEAK DETECTED in response! Key: ${key}. Blocking response.`);
      return true;
    }
  }
  return false;
}

// --- Blocklist (security-sensitive, never proxy) ---
const BLOCKED_HOSTS = new Set([
  '169.254.169.254',          // Cloud instance metadata
  'metadata.google.internal',
  'metadata.hetzner.cloud',
]);

// --- Allowlist Check ---
function isAllowed(hostname, serviceConfig) {
  if (BLOCKED_HOSTS.has(hostname)) return false;
  if (serviceConfig.services && serviceConfig.services[hostname]) return true;
  if (serviceConfig.allowWebBrowsing) return true;
  return false;
}

// --- Inject Credentials ---
function injectCredentials(hostname, headers, serviceConfig, label) {
  if (!serviceConfig.services) return;
  const service = serviceConfig.services[hostname];
  if (!service || !service.header || !service.secretKey) return;
  
  const secret = getSecret(service.secretKey);
  if (!secret) {
    log(`  ⚠️ [${label}] No secret found for key: ${service.secretKey}`);
    return;
  }
  
  const prefix = service.prefix || '';
  headers[service.header.toLowerCase()] = prefix + secret;
  log(`  → [${label}] Injected credential for ${hostname}`);
}

// --- Handle CONNECT (HTTPS tunneling) ---
function handleConnect(req, clientSocket, head) {
  const [hostname, port] = req.url.split(':');
  const targetPort = parseInt(port) || 443;
  const sourceIp = clientSocket.remoteAddress.replace('::ffff:', '');
  const { config: svcConfig, label } = getConfigForSource(sourceIp);
  
  log(`CONNECT [${label}] ${hostname}:${targetPort} (from ${sourceIp})`);
  
  if (!isAllowed(hostname, svcConfig)) {
    log(`  ✗ [${label}] BLOCKED (not in allowlist)`);
    clientSocket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
    clientSocket.end();
    return;
  }
  
  clientSocket.on('error', (e) => {
    log(`  ✗ [${label}] CONNECT client error: ${e.message}`);
    serverSocket && serverSocket.destroy();
  });

  const serverSocket = net.connect(targetPort, hostname, () => {
    clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    serverSocket.write(head);
    serverSocket.pipe(clientSocket);
    clientSocket.pipe(serverSocket);
  });

  serverSocket.on('error', (e) => {
    log(`  ✗ [${label}] CONNECT server error: ${e.message}`);
    clientSocket.destroy();
  });
}

// --- Handle HTTP requests ---
function handleRequest(clientReq, clientRes) {
  const parsedUrl = url.parse(clientReq.url);
  const hostname = parsedUrl.hostname;
  const sourceIp = clientReq.socket.remoteAddress.replace('::ffff:', '');
  const { config: svcConfig, label } = getConfigForSource(sourceIp);
  
  // Always use HTTPS for configured API services
  const service = svcConfig.services ? svcConfig.services[hostname] : null;
  const forceHttps = service && service.header;
  const isHttps = parsedUrl.protocol === 'https:' || forceHttps;
  const targetPort = parseInt(parsedUrl.port) || (isHttps ? 443 : 80);
  
  log(`${clientReq.method} [${label}] ${hostname}${parsedUrl.path} (from ${sourceIp})`);
  
  if (!isAllowed(hostname, svcConfig)) {
    log(`  ✗ [${label}] BLOCKED (not in allowlist)`);
    clientRes.writeHead(403, { 'Content-Type': 'text/plain' });
    clientRes.end('Blocked by credential proxy: host not in allowlist');
    return;
  }
  
  // Copy headers and inject credentials
  const headers = { ...clientReq.headers };
  delete headers['proxy-connection'];
  headers.host = hostname + (targetPort !== 80 && targetPort !== 443 ? `:${targetPort}` : '');
  
  injectCredentials(hostname, headers, svcConfig, label);
  
  const transport = isHttps ? https : http;
  const proxyReq = transport.request({
    hostname,
    port: targetPort,
    path: parsedUrl.path,
    method: clientReq.method,
    headers,
  }, (proxyRes) => {
    const chunks = [];
    proxyRes.on('data', (chunk) => chunks.push(chunk));
    proxyRes.on('end', () => {
      const body = Buffer.concat(chunks);
      
      if (checkForLeaks(body)) {
        clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
        clientRes.end('Blocked by credential proxy: response contained secret material');
        return;
      }
      
      clientRes.writeHead(proxyRes.statusCode, proxyRes.headers);
      clientRes.end(body);
    });
  });
  
  proxyReq.on('error', (e) => {
    log(`  ✗ [${label}] Proxy error: ${e.message}`);
    if (!clientRes.headersSent) {
      clientRes.writeHead(502, { 'Content-Type': 'text/plain' });
      clientRes.end(`Proxy error: ${e.message}`);
    } else {
      clientRes.destroy();
    }
  });

  clientReq.on('error', (e) => {
    log(`  ✗ [${label}] Client request error: ${e.message}`);
    proxyReq.destroy();
  });

  clientReq.pipe(proxyReq);
}

// --- Start ---
loadVault();

const server = http.createServer(handleRequest);
server.on('connect', handleConnect);

server.listen(PORT, '0.0.0.0', () => {
  log(`Credential proxy listening on :${PORT}`);
  log(`Config dir: ${CONFIG_DIR}`);
  log(`Vault: ${VAULT_PATH}`);
  log(`Routing: ${SUBNET_MAP.map(s => `${s.cidr} → ${s.label}`).join(', ')}`);
  log(`OC services: ${Object.keys(configs.oc.services || {}).join(', ')}`);
  log(`CC services: ${Object.keys(configs.cc.services || {}).join(', ')}`);
  log(`Send SIGHUP to reload vault + configs`);
});

// Safety net: log unexpected errors without crashing
process.on('uncaughtException', (err) => {
  log(`UNCAUGHT: ${err.message} (${err.code || 'no code'})`);
});

process.on('SIGTERM', () => { log('Shutting down'); server.close(); process.exit(0); });
process.on('SIGINT', () => { log('Shutting down'); server.close(); process.exit(0); });
