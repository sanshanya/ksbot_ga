#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { Client, Dispatcher, LogLevel } from 'open-event-sdk';
import { normalize } from './wps_event_normalize.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
loadDotenv(path.join(process.cwd(), '..', '.env'));
loadDotenv(path.join(here, '..', '.env'));

const appId = env('APP_ID') || env('WPS365_CLIENT_ID');
const appSecret = env('APP_SECRET') || env('WPS365_CLIENT_SECRET');
const spId = env('WPS365_SP_ID');
const target = env('WPS_EVENT_BRIDGE_TARGET') || 'http://127.0.0.1:23883/wps/callback';
const secret = env('WPS_EVENT_BRIDGE_SECRET') || env('GA_WPS_CALLBACK_SECRET');
if (!appId || !appSecret || !spId) throw new Error('WPS credentials and resolved WPS365_SP_ID are required');
const botIds = [appId, spId];

const dispatcher = new Dispatcher().registerFunc('kso.app_chat.message.create', async event => {
  const data = eventData(event);
  const sender = data?.sender || {};
  if ((sender.type === 'app' || sender.type === 'sp') && [sender.id, sender.app_id].some(id => botIds.includes(id))) return;
  const eventId = event.eventId || event.id || event.uuid || '';
  const payload = normalize(data, botIds, eventId);
  if (!payload.chat_id) return;
  if (!payload.text && !payload.attachments.length && !payload.cloud_docs.length && !payload.shared_docs.length) {
    payload.text = '[non-text message]';
  }
  await postJson(target, payload);
});

const client = new Client({
  appId,
  appSecret,
  dispatcher,
  logLevel: LogLevel.Info,
  reconnectMaxRetry: -1,
});
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
console.log(`[ga-wps-bridge] connecting target=${target}`);
await client.start();

async function shutdown() {
  await client.stop();
  process.exit(0);
}

function eventData(event) {
  const data = event.parsedData ?? event.data;
  if (typeof data !== 'string') return data || {};
  try { return JSON.parse(data); } catch { return {raw: data}; }
}
async function postJson(url, payload) {
  const headers = {'Content-Type': 'application/json'};
  if (secret) headers['X-GA-WPS-SECRET'] = secret;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {
      method: 'POST', headers, body: JSON.stringify(payload), signal: controller.signal
    });
    if (!response.ok) throw new Error(`callback returned ${response.status}: ${await response.text()}`);
  } finally {
    clearTimeout(timer);
  }
}
function loadDotenv(file) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const [left, ...right] = line.replace(/^export\s+/, '').split('=');
    const key = left.trim();
    const value = right.join('=').trim().replace(/^['"]|['"]$/g, '');
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}
function env(name) { return process.env[name]?.trim() || ''; }
