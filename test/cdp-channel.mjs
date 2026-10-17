// Isolated CDP WebSocket transport: deterministic timeout and disconnect semantics.
// No browser, filesystem or third-party dependency; unit-testable with a fake socket.
export async function connectCdp(ws, { connectTimeoutMs = 8000, commandTimeoutMs = 15000 } = {}) {
  if (!Number.isFinite(connectTimeoutMs) || connectTimeoutMs <= 0 ||
      !Number.isFinite(commandTimeoutMs) || commandTimeoutMs <= 0) {
    throw new RangeError("CDP timeout values must be positive finite milliseconds");
  }
  const pending = new Map();
  const listeners = [];
  let seq = 0;
  let closed = false;

  const rejectPending = (reason) => {
    for (const [id, entry] of pending) {
      clearTimeout(entry.timer);
      pending.delete(id);
      entry.reject(reason);
    }
  };
  const fail = (message) => {
    closed = true;
    rejectPending(new Error(message));
  };
  ws.addEventListener("close", () => fail("CDP WebSocket closed"));
  ws.addEventListener("error", () => fail("CDP WebSocket error"));

  try {
    await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("error", onError);
        ws.removeEventListener("close", onClose);
        if (error) reject(error);
        else resolve();
      };
      const onOpen = () => finish();
      const onError = () => finish(new Error("CDP WebSocket connection failed"));
      const onClose = () => finish(new Error("CDP WebSocket closed before open"));
      const timer = setTimeout(() => finish(new Error("CDP WebSocket open timeout")), connectTimeoutMs);
      ws.addEventListener("open", onOpen);
      ws.addEventListener("error", onError);
      ws.addEventListener("close", onClose);
      if (ws.readyState === 1) finish();
    });
  } catch (error) {
    try { ws.close(); } catch { /* ignore */ }
    throw error;
  }

  ws.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      clearTimeout(entry.timer);
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(entry.method + ": " + JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
      return;
    }
    for (const fn of listeners) fn(msg);
  });

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    if (closed || ws.readyState !== 1) {
      reject(new Error("CDP WebSocket not open: " + method));
      return;
    }
    const id = ++seq;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("CDP command timeout: " + method));
    }, commandTimeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    try {
      ws.send(JSON.stringify({ id, method, params }));
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    }
  });
  const close = () => {
    fail("CDP session closed");
    try { ws.close(); } catch { /* ignore */ }
  };
  return {
    send,
    on: (fn) => listeners.push(fn),
    rejectPending,
    close,
  };
}
