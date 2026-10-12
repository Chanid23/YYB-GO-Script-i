#!/usr/bin/env node
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');
const { URL, URLSearchParams } = require('node:url');

const APP_ID = 'wxece3a9a4c82f58c9';
const APP_VERSION = '12.6.3';
const DEVICE_FILE = '.protocol_device.json';
const PARAMS_RESULT_FILE = 'algorithm_params_result.json';
let cachedAwsc = null;
let cachedStorage = new Map();
let cachedDebug = false;

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--')) continue;
    const name = key.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) opts[name] = true;
    else { opts[name] = next; i += 1; }
  }
  return opts;
}

function yes(value) { return value === true || /^(1|true|y|yes)$/i.test(String(value || '').trim()); }
function safeJsonParse(text, fallback = null) { try { return JSON.parse(text); } catch (_) { return fallback; } }
function setGlobal(name, value) { Object.defineProperty(global, name, { value, writable: true, configurable: true, enumerable: true }); }
function log(...args) { if (cachedDebug) console.error(...args); }

function requestText({ url, method = 'GET', headers = {}, body = '', timeout = 10000 }) {
  return new Promise(resolve => {
    let u;
    try { u = new URL(url); } catch (err) { resolve({ ok: false, statusCode: 0, headers: {}, text: '', error: err.message }); return; }
    const lib = u.protocol === 'http:' ? http : https;
    const req = lib.request({ protocol: u.protocol, hostname: u.hostname, port: u.port, path: u.pathname + u.search, method, headers, timeout }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, statusCode: res.statusCode || 0, headers: res.headers, text: Buffer.concat(chunks).toString('utf8') }));
    });
    req.on('error', err => resolve({ ok: false, statusCode: 0, headers: {}, text: '', error: err.message }));
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (body) req.write(body);
    req.end();
  });
}

function makeWxEnv({ debug = false, useNetwork = true } = {}) {
  cachedDebug = debug;
  const storage = cachedStorage;
  const info = {
    brand: 'microsoft', model: 'microsoft', system: 'windows/Unknown', platform: 'windows',
    SDKVersion: '3.8.12', version: '4.1.11.24', language: 'zh_CN', pixelRatio: 1,
    screenWidth: 390, screenHeight: 844, windowWidth: 390, windowHeight: 844,
    statusBarHeight: 20, benchmarkLevel: 100, fontSizeSetting: 16,
    bluetoothEnabled: true, locationEnabled: true, wifiEnabled: true,
    miniProgram: { appId: APP_ID, version: APP_VERSION, envVersion: 'release' },
    safeArea: { left: 0, right: 390, top: 20, bottom: 844, width: 390, height: 824 }
  };
  const asyncOk = (opt, data) => { if (opt && typeof opt.success === 'function') opt.success(data); if (opt && typeof opt.complete === 'function') opt.complete(data); return Promise.resolve(data); };
  const wxBase = {
    mor_modules: [],
    getSystemInfoSync() { return info; }, getSystemInfo(opt) { return asyncOk(opt, info); }, getSystemInfoAsync() { return Promise.resolve(info); },
    getAccountInfoSync() { return { miniProgram: info.miniProgram }; },
    getLaunchOptionsSync() { return { scene: 1001, path: 'pages/index/index', query: {}, referrerInfo: {} }; },
    getEnterOptionsSync() { return { scene: 1001, path: 'pages/index/index', query: {}, referrerInfo: {} }; },
    getStorageSync(key) { return storage.get(key) || ''; }, setStorageSync(key, value) { storage.set(key, value); }, removeStorageSync(key) { storage.delete(key); },
    getStorage(opt) { return asyncOk(opt, { data: storage.get(opt && opt.key) || '' }); },
    setStorage(opt) { if (opt) storage.set(opt.key, opt.data); return asyncOk(opt, { errMsg: 'setStorage:ok' }); },
    removeStorage(opt) { if (opt) storage.delete(opt.key); return asyncOk(opt, { errMsg: 'removeStorage:ok' }); },
    canIUse() { return true; }, getNetworkType(opt) { return asyncOk(opt, { networkType: 'wifi' }); }, onNetworkStatusChange() {}, offNetworkStatusChange() {},
    getBatteryInfoSync() { return { level: 90, isCharging: true }; }, getBatteryInfo(opt) { return asyncOk(opt, { level: 90, isCharging: true }); },
    getScreenRecordingState(opt) { return asyncOk(opt, { state: 'off' }); }, getLocalIPAddress(opt) { return asyncOk(opt, { localip: '192.168.1.2' }); },
    getRendererUserAgent(opt) { return asyncOk(opt, { userAgent: 'Mozilla/5.0 MiniProgramEnv/Windows MicroMessenger/4.1.11.24' }); },
    getDeviceInfo() { return info; }, getDeviceBenchmarkInfo() { return { benchmarkLevel: 100 }; }, getDeviceBaseInfo() { return info; },
    getConnectedWifi(opt) { return asyncOk(opt, { wifi: { SSID: '', BSSID: '', secure: true, signalStrength: 99 } }); },
    getWindowInfo() { return info; }, getAppBaseInfo() { return { SDKVersion: info.SDKVersion, version: info.version, language: 'zh_CN' }; },
    login(opt) { return asyncOk(opt, { code: 'TEST_WX_LOGIN_CODE' }); },
    getRandomValues(arr) { return crypto.webcrypto.getRandomValues(arr); },
    request(opt) {
      const reqUrl = String((opt && opt.url) || '');
      const method = String((opt && opt.method) || 'GET').toUpperCase();
      const data = opt && opt.data;
      const body = typeof data === 'string' ? data : (data ? new URLSearchParams(data).toString() : '');
      log('[awsc wx.request]', method, reqUrl);
      if (!useNetwork) { const pack = { statusCode: 0, header: {}, headers: {}, data: '' }; if (opt && opt.fail) opt.fail(pack); if (opt && opt.complete) opt.complete(pack); return; }
      requestText({ url: reqUrl, method, headers: (opt && (opt.header || opt.headers)) || {}, body, timeout: 10000 }).then(res => {
        let outData = res.text;
        if (String((opt && opt.dataType) || '').toLowerCase() === 'json') outData = safeJsonParse(res.text, res.text);
        const pack = { statusCode: res.statusCode, header: res.headers, headers: res.headers, data: outData };
        if (res.ok && opt && opt.success) opt.success(pack);
        if (!res.ok && opt && opt.fail) opt.fail(pack);
        if (opt && opt.complete) opt.complete(pack);
      }).catch(err => {
        const pack = { statusCode: 0, header: {}, headers: {}, data: '', errMsg: err.message };
        if (opt && opt.fail) opt.fail(pack);
        if (opt && opt.complete) opt.complete(pack);
      });
    }
  };
  const wx = new Proxy(wxBase, { get(target, prop) { if (prop in target) return target[prop]; if (typeof prop === 'symbol') return target[prop]; const name = String(prop); if (name.startsWith('on') || name.startsWith('off')) return function noop() {}; return target[prop]; } });

  setGlobal('wx', wx); setGlobal('my', wx); setGlobal('window', global); setGlobal('self', global);
  setGlobal('navigator', { userAgent: 'Mozilla/5.0 MiniProgramEnv/Windows MicroMessenger/4.1.11.24', language: 'zh-CN', platform: 'Win32' });
  setGlobal('document', { cookie: '', createElement() { return {}; } });
  setGlobal('location', { href: `https://servicewechat.com/${APP_ID}/831/page-frame.html`, protocol: 'https:', host: 'servicewechat.com' });
  setGlobal('crypto', crypto.webcrypto); setGlobal('Page', page => { global.__awscPage = page; return page; }); setGlobal('App', app => app);
  setGlobal('Component', component => component); setGlobal('Behavior', behavior => behavior);
  setGlobal('getCurrentPages', () => [{ route: 'pages/index/index', __route__: 'pages/index/index', options: {} }]);
  setGlobal('getApp', () => ({ globalData: {}, MOR_APP_CONFIG: { appId: APP_ID, version: APP_VERSION } }));
  setGlobal('requirePlugin', name => { log('[requirePlugin]', name); return require(path.join(process.cwd(), 'plugin_', 'wxd46b6e9c9b775a56', 'fireyejs.min.js')); });

  if (!global.__awscSetIntervalPatched) {
    const realSetInterval = global.setInterval;
    global.setInterval = function patchedSetInterval(...args) { const timer = realSetInterval(...args); if (timer && timer.unref) timer.unref(); return timer; };
    global.__awscSetIntervalPatched = true;
  }
  return wx;
}

function loadAwscWrapper({ debug = false, useNetwork = true } = {}) {
  cachedDebug = debug;
  if (cachedAwsc) return cachedAwsc;
  const wx = makeWxEnv({ debug, useNetwork });
  require(path.join(process.cwd(), 'mor.v.js'));
  const pushed = wx.mor_modules && wx.mor_modules[0];
  if (!pushed || !pushed[1] || !pushed[1]['87GW']) throw new Error('mor.v.js module 87GW not found');
  const modules = pushed[1];
  const cache = {};
  function req(id) { if (cache[id]) return cache[id].exports; if (!modules[id]) return require(id); const mod = { id, loaded: false, exports: {} }; cache[id] = mod; modules[id](mod, mod.exports, req); mod.loaded = true; return mod.exports; }
  req.d = (exports, definition) => { for (const key of Object.keys(definition)) if (!Object.prototype.hasOwnProperty.call(exports, key)) Object.defineProperty(exports, key, { enumerable: true, get: definition[key] }); };
  req.r = exports => { Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' }); Object.defineProperty(exports, '__esModule', { value: true }); };
  req.n = mod => { const getter = mod && mod.__esModule ? () => mod.default : () => mod; req.d(getter, { a: getter }); return getter; };
  req.nmd = mod => { mod.paths = []; if (!mod.children) mod.children = []; return mod; };
  cachedAwsc = req('87GW');
  cachedAwsc.init({ appName: 'eleme', filterJanus() { return true; }, filterUab() { return true; }, filterUmid() { return true; } });
  return cachedAwsc;
}

function buildBxUaDirect({ debug = false } = {}) {
  try {
    // bx-ua 直接走 awscwx 插件生成，比 mor.v.js wrapper 的 getFYToken 更稳。
    const pluginPath = path.join(process.cwd(), 'plugin_', 'wxd46b6e9c9b775a56', 'fireyejs.min.js');
    const resolved = require.resolve(pluginPath);

    // 重新加载一次插件，避免 wrapper 入口初始化状态污染。
    delete require.cache[resolved];
    const plugin = require(resolved);
    const wxObj = global.wx;
    const env = {
      env: { obj: wxObj, getCurrentPages: global.getCurrentPages },
      obj: wxObj,
      getCurrentPages: global.getCurrentPages
    };
    const cfg = {
      appName: 'eleme',
      appId: APP_ID,
      miniAppId: APP_ID,
      platform: 'wx',
      appEntrance: 'weixin',
      channel: 'wechat_app'
    };
    if (plugin && typeof plugin.init === 'function') plugin.init(env, cfg, env, '');
    const value = plugin && typeof plugin.getFYToken === 'function' ? plugin.getFYToken() : '';
    return String(value || '');
  } catch (err) {
    if (debug) console.error('[direct bx-ua failed]', err && err.stack ? err.stack : err);
    return '';
  }
}
function extractMuToken(text) { const match = String(text || '').match(/(?:umx\.wu|__fycb)\(['"]([^'"]+)['"]\)/); return match ? match[1] : ''; }
async function fetchUmidDirect() { const res = await requestText({ url: 'https://ynuf.aliapp.org/w/mu.json', method: 'POST', timeout: 10000 }); return extractMuToken(res.text); }

async function getUmidToken(awsc, { useNetwork = true, timeout = 12000 } = {}) {
  const syncToken = String((awsc.getUmidTokenSync && awsc.getUmidTokenSync()) || (awsc.getUidTokenSync && awsc.getUidTokenSync()) || '');
  if (syncToken) return syncToken;
  if (!useNetwork) return '';
  return new Promise(resolve => {
    let done = false;
    const finish = value => { const token = String(value || ''); if (done || !token) return; done = true; resolve(token); };
    const timer = setTimeout(() => { if (!done) { done = true; resolve(''); } }, timeout);
    if (timer && timer.unref) timer.unref();
    try { if (awsc.getUmidTokenAsyn) awsc.getUmidTokenAsyn(value => finish(value)); } catch (_) {}
    try { if (awsc.getUidTokenAsyn) awsc.getUidTokenAsyn(value => finish(value)); } catch (_) {}
    fetchUmidDirect().then(value => finish(value)).catch(() => {});
  });
}

function loadOrCreateDevice() {
  const file = path.join(process.cwd(), DEVICE_FILE);
  const old = fs.existsSync(file) ? safeJsonParse(fs.readFileSync(file, 'utf8'), {}) : {};
  if (old && old.utdid && old.deviceId) return old;
  const device = { utdid: old.utdid || crypto.randomUUID().replace(/-/g, '').toUpperCase(), deviceId: old.deviceId || crypto.randomBytes(16).toString('hex').toUpperCase(), createdAt: old.createdAt || new Date().toISOString() };
  fs.writeFileSync(file, JSON.stringify(device, null, 2), 'utf8');
  return device;
}

function sessionToXSmallstc(session) { const copy = { ...(session || {}) }; delete copy.username; return Object.keys(copy).length ? JSON.stringify(copy) : ''; }
function buildXEleUa({ session = {}, lng = '', lat = '', brand = 'microsoft', model = 'microsoft', system = 'windows/Unknown', wechatVersion = '4.1.11.24' } = {}) {
  const deviceId = session.union_id || session.unionId || session.open_id || session.user_id || '';
  return ['RenderWay/miniProgram', `MiniAppId/${APP_ID}`, `MiniAppVersion/${APP_VERSION}`, deviceId ? `DeviceId/${deviceId}` : '', 'AppName/Wechat', `${brand}/${model}/${system}`, `Wechat/${wechatVersion}`, 'channel/wechat_app', 'subChannel/wechat_app.default', lng && lat ? `Longitude/${lng}` : '', lng && lat ? `Latitude/${lat}` : ''].filter(Boolean).join(' ');
}

async function buildAlgorithmParams(rawOpts = {}) {
  const debug = yes(rawOpts.debug);
  const useNetwork = !yes(rawOpts['no-network'] || rawOpts.noNetwork);
  const session = rawOpts.session && typeof rawOpts.session === 'object' ? rawOpts.session : {};
  const url = String(rawOpts.url || rawOpts.requestUrl || 'https://waimai-guide.ele.me/h5/mtop.alsc.user.session.exchange.apply/1.0/2.0/');
  const awsc = loadAwscWrapper({ debug, useNetwork });
  const miniJanusRaw = String((awsc.getSign && awsc.getSign(url)) || '');
  const miniJanus = miniJanusRaw ? encodeURIComponent(miniJanusRaw) : '';
  const bxUa = String(buildBxUaDirect({ debug }) || (awsc.getFYToken && awsc.getFYToken()) || '');
  const bxUmidToken = String(rawOpts.umidToken || rawOpts['bx-umidtoken'] || await getUmidToken(awsc, { useNetwork }) || '');
  const xSmallstc = rawOpts.xSmallstc || sessionToXSmallstc(session);
  const xEleUa = rawOpts.xEleUa || buildXEleUa({ session, lng: rawOpts.lng || rawOpts.longitude || '', lat: rawOpts.lat || rawOpts.latitude || '', brand: rawOpts.brand || 'microsoft', model: rawOpts.model || 'microsoft', system: rawOpts.system || 'windows/Unknown', wechatVersion: rawOpts.wechatVersion || rawOpts.wechat || '4.1.11.24' });
  const device = loadOrCreateDevice();
  const xElemeRequestId = rawOpts.requestId || `${crypto.randomBytes(16).toString('hex').toUpperCase()}|${Date.now()}`;
  const xUid = rawOpts.uid || session.munb || session.user_id || session.userId || session.USERID || '';
  const xMiniWua = rawOpts.xMiniWua || rawOpts['x-mini-wua'] || '';
  const headers = {};
  if (miniJanus) headers['mini-janus'] = miniJanus;
  if (bxUa) headers['bx-ua'] = bxUa;
  if (bxUmidToken) { headers['bx-umidtoken'] = bxUmidToken; headers['x-umidToken'] = bxUmidToken; }
  if (xSmallstc) headers['x-smallstc'] = xSmallstc;
  if (xEleUa) headers['x-ele-ua'] = xEleUa;
  if (xElemeRequestId) headers['x-eleme-requestid'] = xElemeRequestId;
  if (xUid) headers['x-uid'] = String(xUid);
  if (device.utdid) headers['x-utdid'] = device.utdid;
  if (xMiniWua) headers['x-mini-wua'] = xMiniWua;
  return {
    ok: Boolean(miniJanus && bxUa && bxUmidToken),
    source: { awscWrapper: 'mor.v.js module 87GW', awscPlugin: 'plugin_/wxd46b6e9c9b775a56/fireyejs.min.js', umidEndpoint: useNetwork ? 'https://ynuf.aliapp.org/w/mu.json' : null },
    params: { miniJanus, miniJanusRaw, bxUa, bxUmidToken, xUmidToken: bxUmidToken, xSmallstc, xEleUa, xMiniWua, xElemeRequestId, xUid: String(xUid || ''), xUtdid: device.utdid, awscJsv: awsc.jsv || null, appId: APP_ID, appVersion: APP_VERSION },
    headers,
    notes: { miniJanus: 'getSign(url) then encodeURIComponent.', bxUa: 'getFYToken().', bxUmidToken: 'getUmidTokenAsyn/getUidTokenAsyn or mu.json.', xMiniWua: xMiniWua ? 'Provided by input.' : 'Native WUA is not available in unpacked JS, empty by default.' }
  };
}

async function cli() {
  const opts = parseArgs(process.argv.slice(2));
  let session = {};
  const sessionFile = String(opts['session-file'] || opts.sessionFile || '').trim();
  if (sessionFile && fs.existsSync(sessionFile)) { const loaded = safeJsonParse(fs.readFileSync(sessionFile, 'utf8'), {}); session = loaded.session || loaded.data || loaded; }
  opts.session = session;
  const result = await buildAlgorithmParams(opts);
  const out = String(opts.out || PARAMS_RESULT_FILE);
  fs.writeFileSync(out, JSON.stringify(result, null, 2), 'utf8');
  console.log(JSON.stringify(result, null, 2));
  console.error(`\nResult saved: ${out}`);
}

if (require.main === module) cli().catch(err => { console.error('Build algorithm params failed:', err && err.stack ? err.stack : err); process.exit(1); });
module.exports = { buildAlgorithmParams, buildXEleUa, sessionToXSmallstc };
