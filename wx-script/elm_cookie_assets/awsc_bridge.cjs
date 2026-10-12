#!/usr/bin/env node
'use strict';

/**
 * awsc_bridge.cjs —— 供 Python 纯协议客户端调用的算法参数桥。
 *
 * 只负责生成必须依赖阿里 fireye / AWSC 加固 SDK 的参数：
 *   - mini-janus  (mor.v.js  getSign)
 *   - bx-ua       (fireyejs.min.js  getFYToken)
 *   - bx-umidtoken(getUmidToken / mu.json)
 * 其余 header（x-ele-ua / x-smallstc / x-utdid / x-eleme-requestid / x-uid）
 * 也一并由 awsc_params.cjs 组装返回，Python 侧直接取用即可。
 *
 * 用法（由 eleme_login.py 自动调用，一般无需手动执行）：
 *   node awsc_bridge.cjs <argsJsonFile> <outJsonFile>
 *
 * argsJsonFile 内容示例：
 *   { "url": "https://...", "session": {}, "umidToken": "", "noNetwork": false, "debug": false }
 *
 * 结果写入 outJsonFile（纯 JSON），避免 mor.v.js 加载时的任何 stdout 噪声污染结果。
 */

const fs = require('node:fs');
const path = require('node:path');
const { buildAlgorithmParams } = require('./awsc_params.cjs');

async function main() {
  const argsFile = process.argv[2];
  const outFile = process.argv[3];
  if (!argsFile || !outFile) {
    console.error('usage: node awsc_bridge.cjs <argsJsonFile> <outJsonFile>');
    process.exit(2);
  }

  let opts = {};
  try {
    opts = JSON.parse(fs.readFileSync(argsFile, 'utf8').replace(/^﻿/, '')) || {};
  } catch (err) {
    fs.writeFileSync(outFile, JSON.stringify({ ok: false, error: `bad args json: ${err.message}` }), 'utf8');
    process.exit(1);
  }

  try {
    const result = await buildAlgorithmParams({
      url: opts.url,
      session: opts.session && typeof opts.session === 'object' ? opts.session : {},
      umidToken: opts.umidToken || '',
      debug: Boolean(opts.debug),
      noNetwork: Boolean(opts.noNetwork),
      // 允许透传其它可选项（lng/lat/uid/requestId/xMiniWua...）
      ...opts
    });
    fs.writeFileSync(outFile, JSON.stringify(result), 'utf8');
    process.exit(0);
  } catch (err) {
    fs.writeFileSync(outFile, JSON.stringify({ ok: false, error: err && err.stack ? err.stack : String(err) }), 'utf8');
    process.exit(1);
  }
}

main();
