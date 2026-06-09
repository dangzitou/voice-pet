const fs = require('fs');
const path = require('path');
const readline = require('readline');
const WebSocket = require('ws');

const DEFAULT_RECONNECT_MS = 2000;
const DEFAULT_PING_INTERVAL_MS = 30000;

function main() {
  const [, , rawUrl, token, sessionId, progressPath] = process.argv;
  if (!rawUrl || !token || !sessionId) {
    console.error('usage: node pico_bridge_once.js <url> <token> <sessionId> [progressPath]');
    process.exit(2);
  }

  const state = {
    rawUrl,
    token,
    sessionId,
    progressPath: progressPath || '',
    ws: null,
    pingTimer: null,
    reconnectTimer: null,
    connecting: false,
    connected: false,
    pendingById: new Map(),
    queue: [],
    shuttingDown: false,
  };

  setupInput(state);
  connect(state);
}

function setupInput(state) {
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch (err) {
      emit({
        type: 'error',
        request_id: '',
        error: `invalid stdin json: ${String(err.message || err)}`,
      });
      return;
    }
    handleCommand(state, msg);
  });

  rl.on('close', () => {
    shutdown(state, 0);
  });
}

function handleCommand(state, msg) {
  const action = String(msg.action || '').trim();
  if (action === 'send') {
    const requestId = String(msg.request_id || '').trim();
    const content = String(msg.content || '').trim();
    const timeoutSeconds = Math.max(1, Number(msg.timeout_seconds || 30) || 30);
    if (!requestId || !content) {
      emit({
        type: 'error',
        request_id: requestId,
        error: 'send requires request_id and content',
      });
      return;
    }
    const request = {
      action,
      request_id: requestId,
      content,
      timeout_ms: Math.round(timeoutSeconds * 1000),
    };
    state.pendingById.set(requestId, request);
    armTimeout(state, request);
    enqueueOrSend(state, request);
    return;
  }

  if (action === 'cancel') {
    const requestId = String(msg.request_id || '').trim();
    const request = state.pendingById.get(requestId);
    if (!request) return;
    clearRequest(request);
    state.pendingById.delete(requestId);
    state.queue = state.queue.filter((item) => item.request_id !== requestId);
    emit({
      type: 'cancelled',
      request_id: requestId,
    });
    return;
  }

  if (action === 'shutdown') {
    shutdown(state, 0);
    return;
  }

  emit({
    type: 'error',
    request_id: String(msg.request_id || ''),
    error: `unsupported action: ${action}`,
  });
}

function enqueueOrSend(state, request) {
  if (!state.connected || !state.ws || state.ws.readyState !== WebSocket.OPEN) {
    state.queue.push(request);
    connect(state);
    return;
  }
  sendRequest(state, request);
}

function sendRequest(state, request) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    state.queue.push(request);
    return;
  }
  state.ws.send(JSON.stringify({
    type: 'message.send',
    session_id: state.sessionId,
    payload: { content: request.content },
  }));
}

function connect(state) {
  if (state.shuttingDown || state.connecting || state.connected) return;
  state.connecting = true;

  const url = new URL(state.rawUrl);
  url.searchParams.set('session_id', state.sessionId);

  const ws = new WebSocket(url, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  state.ws = ws;

  ws.on('open', () => {
    state.connecting = false;
    state.connected = true;
    startPing(state);
    emit({ type: 'status', status: 'connected' });
    flushQueue(state);
  });

  ws.on('message', (buf) => {
    handleServerMessage(state, buf.toString());
  });

  ws.on('close', () => {
    onDisconnect(state);
  });

  ws.on('error', (err) => {
    emit({
      type: 'status',
      status: 'error',
      error: String((err && err.message) || err),
    });
  });
}

function onDisconnect(state) {
  stopPing(state);
  state.connected = false;
  state.connecting = false;
  if (state.shuttingDown) return;
  emit({ type: 'status', status: 'disconnected' });
  if (state.reconnectTimer) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connect(state);
  }, DEFAULT_RECONNECT_MS);
  if (typeof state.reconnectTimer.unref === 'function') {
    state.reconnectTimer.unref();
  }
}

function flushQueue(state) {
  const queued = state.queue;
  state.queue = [];
  for (const request of queued) {
    if (!state.pendingById.has(request.request_id)) continue;
    sendRequest(state, request);
  }
}

function handleServerMessage(state, raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (_err) {
    return;
  }
  if (msg.type !== 'message.create') return;

  const payload = msg.payload || {};
  const kind = String(payload.kind || '').toLowerCase().trim();
  const reply = String(payload.content || '').trim();

  if (kind === 'tool_calls') {
    writeProgress(state.progressPath, summarizeToolCalls(payload.tool_calls));
    emit({
      type: 'progress',
      kind,
      text: summarizeToolCalls(payload.tool_calls).text,
    });
    return;
  }
  if (kind === 'thought') {
    writeProgress(state.progressPath, { text: '我正在整理思路', kind });
    emit({
      type: 'progress',
      kind,
      text: '我正在整理思路',
    });
    return;
  }
  if (!reply) return;

  const pending = oldestPending(state);
  if (pending) {
    clearRequest(pending);
    state.pendingById.delete(pending.request_id);
    emit({
      type: 'reply',
      request_id: pending.request_id,
      text: reply,
    });
    return;
  }

  emit({
    type: 'push',
    text: reply,
  });
}

function oldestPending(state) {
  let oldest = null;
  for (const request of state.pendingById.values()) {
    if (!oldest || request.started_at < oldest.started_at) {
      oldest = request;
    }
  }
  return oldest;
}

function armTimeout(state, request) {
  request.started_at = Date.now();
  request.timer = setTimeout(() => {
    state.pendingById.delete(request.request_id);
    state.queue = state.queue.filter((item) => item.request_id !== request.request_id);
    emit({
      type: 'timeout',
      request_id: request.request_id,
      error: 'timeout waiting for reply',
    });
  }, request.timeout_ms);
  if (typeof request.timer.unref === 'function') {
    request.timer.unref();
  }
}

function clearRequest(request) {
  if (request && request.timer) {
    clearTimeout(request.timer);
    request.timer = null;
  }
}

function startPing(state) {
  stopPing(state);
  state.pingTimer = setInterval(() => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    try {
      state.ws.ping();
    } catch (_err) {
      // let the close/error path handle it
    }
  }, DEFAULT_PING_INTERVAL_MS);
  if (typeof state.pingTimer.unref === 'function') {
    state.pingTimer.unref();
  }
}

function stopPing(state) {
  if (state.pingTimer) {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }
}

function shutdown(state, code) {
  if (state.shuttingDown) return;
  state.shuttingDown = true;
  stopPing(state);
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  for (const request of state.pendingById.values()) {
    clearRequest(request);
  }
  state.pendingById.clear();
  if (state.ws) {
    try {
      state.ws.close();
    } catch (_err) {
      // ignore
    }
  }
  process.exit(code);
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function writeProgress(progressPath, payload) {
  if (!progressPath || !payload || !payload.text) return;
  const body = {
    ...payload,
    updated_at: Date.now() / 1000,
  };
  try {
    fs.mkdirSync(path.dirname(progressPath), { recursive: true });
    const tmpPath = `${progressPath}.${process.pid}.tmp`;
    fs.writeFileSync(tmpPath, JSON.stringify(body), 'utf8');
    fs.renameSync(tmpPath, progressPath);
  } catch (_err) {
    // best effort
  }
}

function summarizeToolCalls(rawToolCalls) {
  const calls = normalizeToolCalls(rawToolCalls);
  const names = calls
    .map(toolName)
    .filter(Boolean)
    .map((name) => name.toLowerCase());
  const args = calls
    .map(toolArguments)
    .filter(Boolean)
    .map((value) => value.toLowerCase());
  const joined = [...names, ...args].join(' ');
  let text = '我正在调用工具处理';
  if (/(netease|ncm|music|song|playlist|网易|音乐|歌曲)/.test(joined)) {
    text = '我正在处理音乐播放';
  } else if (/(weather|forecast|天气|气象)/.test(joined)) {
    text = '我正在查询天气';
  } else if (/(web|search|fetch|browser|news|open-websearch|网页|搜索|新闻)/.test(joined)) {
    text = '我正在查网页资料';
  } else if (/(calendar|schedule|日程|提醒|闹钟)/.test(joined)) {
    text = '我正在处理提醒日程';
  } else if (/(file|read|write|list|grep|rg|本地|文件)/.test(joined)) {
    text = '我正在查看本地信息';
  } else if (/(shell|bash|exec|command|terminal|命令)/.test(joined)) {
    text = '我正在执行本地命令';
  } else if (names.length > 1) {
    text = '我正在调用几个工具处理';
  }
  return {
    text,
    kind: 'tool_calls',
    tool_names: names.slice(0, 8),
  };
}

function normalizeToolCalls(rawToolCalls) {
  if (!rawToolCalls) return [];
  if (Array.isArray(rawToolCalls)) return rawToolCalls;
  if (typeof rawToolCalls === 'string') {
    try {
      const parsed = JSON.parse(rawToolCalls);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }
  return [];
}

function toolName(call) {
  if (!call || typeof call !== 'object') return '';
  if (typeof call.name === 'string') return call.name;
  if (typeof call.tool_name === 'string') return call.tool_name;
  const fn = call.function;
  if (fn && typeof fn === 'object' && typeof fn.name === 'string') return fn.name;
  return '';
}

function toolArguments(call) {
  if (!call || typeof call !== 'object') return '';
  const values = [];
  for (const key of ['arguments', 'args', 'input', 'command']) {
    if (typeof call[key] === 'string') values.push(call[key]);
  }
  const fn = call.function;
  if (fn && typeof fn === 'object') {
    for (const key of ['arguments', 'args', 'input', 'command']) {
      if (typeof fn[key] === 'string') values.push(fn[key]);
    }
  }
  return values.join(' ');
}

main();
