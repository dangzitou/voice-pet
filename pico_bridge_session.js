const WebSocket = require('ws');
const readline = require('readline');

function main() {
  const [, , rawUrl, token, sessionId, timeoutArg, progressPath] = process.argv;
  if (!rawUrl || !token || !sessionId) {
    console.error('usage: node pico_bridge_session.js <url> <token> <sessionId> [timeoutSeconds] [progressPath]');
    process.exit(2);
  }

  const state = {
    rawUrl,
    token,
    sessionId,
    timeoutMs: Math.max(1000, Math.round((Number(timeoutArg || '30') || 30) * 1000)),
    progressPath,
    ws: null,
    connected: false,
    pending: null,
    outboundQueue: [],
    reconnectTimer: null,
    reconnectDelayMs: 1000,
  };

  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  rl.on('line', (line) => {
    const raw = String(line || '').trim();
    if (!raw) return;
    let message;
    try {
      message = JSON.parse(raw);
    } catch (err) {
      emit({ type: 'error', message: 'invalid JSON command from python side' });
      return;
    }
    if (message.type !== 'request' || !message.request_id || !message.content) {
      emit({ type: 'error', request_id: String(message.request_id || ''), message: 'invalid request payload' });
      return;
    }
    queueRequest(state, {
      requestId: String(message.request_id),
      content: String(message.content),
    });
  });

  rl.on('close', () => {
    shutdown(state, 0);
  });

  connect(state);
}

function connect(state) {
  const url = new URL(state.rawUrl);
  url.searchParams.set('session_id', state.sessionId);

  const ws = new WebSocket(url, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  state.ws = ws;

  ws.on('open', () => {
    state.connected = true;
    state.reconnectDelayMs = 1000;
    flushQueue(state);
  });

  ws.on('message', (buf) => {
    handleIncoming(state, String(buf || ''));
  });

  ws.on('close', () => {
    const hadPending = Boolean(state.pending);
    state.connected = false;
    state.ws = null;
    if (hadPending) {
      emit({
        type: 'error',
        request_id: state.pending.requestId,
        message: 'pico websocket disconnected while waiting for reply',
      });
      state.pending = null;
    }
    scheduleReconnect(state);
  });

  ws.on('error', (err) => {
    console.error(String((err && err.message) || err));
  });
}

function shutdown(state, code) {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  if (state.ws) {
    try {
      state.ws.close();
    } catch (_err) {
      // ignore close errors
    }
  }
  process.exit(code);
}

function scheduleReconnect(state) {
  if (state.reconnectTimer) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connect(state);
  }, state.reconnectDelayMs);
  if (typeof state.reconnectTimer.unref === 'function') {
    state.reconnectTimer.unref();
  }
  state.reconnectDelayMs = Math.min(state.reconnectDelayMs * 2, 5000);
}

function queueRequest(state, request) {
  state.outboundQueue.push(request);
  flushQueue(state);
}

function flushQueue(state) {
  if (!state.connected || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  if (state.pending || state.outboundQueue.length === 0) return;

  const next = state.outboundQueue.shift();
  state.pending = {
    requestId: next.requestId,
    timeout: setTimeout(() => {
      if (!state.pending || state.pending.requestId !== next.requestId) return;
      emit({
        type: 'error',
        request_id: next.requestId,
        message: 'timeout waiting for PicoClaw reply',
      });
      state.pending = null;
      flushQueue(state);
    }, state.timeoutMs),
  };

  state.ws.send(JSON.stringify({
    type: 'message.send',
    session_id: state.sessionId,
    payload: { content: next.content },
  }));
}

function handleIncoming(state, raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (_err) {
    return;
  }
  if (!msg || msg.type !== 'message.create') return;

  const payload = msg.payload || {};
  const kind = String(payload.kind || '').toLowerCase().trim();
  const content = String(payload.content || '').trim();

  if (kind === 'tool_calls') {
    emit(summarizeProgress(payload.tool_calls));
    return;
  }
  if (kind === 'thought') {
    emit({ type: 'progress', text: '我正在整理思路', kind });
    return;
  }
  if (!content) return;

  if (state.pending) {
    clearTimeout(state.pending.timeout);
    emit({
      type: 'reply',
      request_id: state.pending.requestId,
      content,
    });
    state.pending = null;
    flushQueue(state);
    return;
  }

  emit({
    type: 'push',
    content,
  });
}

function summarizeProgress(rawToolCalls) {
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
  } else if (/(calendar|schedule|日程|提醒)/.test(joined)) {
    text = '我正在查看日程';
  } else if (/(file|read|write|list|grep|rg|本地|文件)/.test(joined)) {
    text = '我正在查看本地信息';
  } else if (/(shell|bash|exec|command|terminal|命令)/.test(joined)) {
    text = '我正在执行本地命令';
  } else if (names.length > 1) {
    text = '我正在调用几个工具处理';
  }
  return {
    type: 'progress',
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

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

main();
