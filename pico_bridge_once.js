const fs = require('fs');
const path = require('path');
const WebSocket = require('ws');

function main() {
  const [, , rawUrl, token, sessionId, content, timeoutArg, progressPath] = process.argv;
  if (!rawUrl || !token || !sessionId || !content) {
    console.error('usage: node pico_bridge_once.js <url> <token> <sessionId> <content> [timeoutSeconds] [progressPath]');
    process.exit(2);
  }

  const timeoutMs = Math.max(1000, Math.round((Number(timeoutArg || '30') || 30) * 1000));
  const url = new URL(rawUrl);
  url.searchParams.set('session_id', sessionId);

  const ws = new WebSocket(url, {
    headers: { Authorization: `Bearer ${token}` },
  });

  const timeout = setTimeout(() => {
    console.error('timeout waiting for reply');
    ws.close();
    process.exit(3);
  }, timeoutMs);

  ws.on('open', () => {
    ws.send(JSON.stringify({
      type: 'message.send',
      session_id: sessionId,
      payload: { content },
    }));
  });

  ws.on('message', (buf) => {
    try {
      const msg = JSON.parse(buf.toString());
      if (msg.type !== 'message.create') return;
      const payload = msg.payload || {};
      const kind = String(payload.kind || '').toLowerCase().trim();
      const reply = String(payload.content || '').trim();
      if (kind === 'tool_calls') {
        writeProgress(progressPath, summarizeToolCalls(payload.tool_calls));
        return;
      }
      if (kind === 'thought') {
        writeProgress(progressPath, { text: '我正在整理思路', kind });
        return;
      }
      if (!reply) return;
      clearTimeout(timeout);
      console.log(reply);
      ws.close();
      process.exit(0);
    } catch (err) {
      // ignore malformed frames
    }
  });

  ws.on('error', (err) => {
    clearTimeout(timeout);
    console.error(String((err && err.message) || err));
    process.exit(1);
  });
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
    // Progress is best effort; final reply should not fail because of it.
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
