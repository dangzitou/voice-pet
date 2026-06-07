const WebSocket = require('ws');

function main() {
  const [, , rawUrl, token, sessionId, content, timeoutArg] = process.argv;
  if (!rawUrl || !token || !sessionId || !content) {
    console.error('usage: node pico_bridge_once.js <url> <token> <sessionId> <content> [timeoutSeconds]');
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
      if (!reply || kind === 'thought' || kind === 'tool_calls') return;
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

main();
