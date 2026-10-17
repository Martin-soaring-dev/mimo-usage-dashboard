// Deterministic offline regression tests for CDP timeout/failure paths.
import assert from "node:assert/strict";
import { connectCdp } from "./cdp-channel.mjs";

class FakeSocket extends EventTarget {
  readyState = 0;
  static OPEN = 1;
  sent = [];
  throwSend = false;
  open() { this.readyState = 1; this.dispatchEvent(new Event("open")); }
  send(payload) {
    if (this.throwSend) throw new Error("synthetic send failure");
    this.sent.push(JSON.parse(payload));
  }
  message(data) {
    const ev = new Event("message");
    Object.defineProperty(ev, "data", { value: JSON.stringify(data) });
    this.dispatchEvent(ev);
  }
  close() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
}
const options = { connectTimeoutMs: 25, commandTimeoutMs: 25 };
const run = async (name, test) => {
  await test();
  console.log("PASS", name);
};

await run("connection never opens: bounded timeout", async () => {
  const ws = new FakeSocket();
  await assert.rejects(connectCdp(ws, options), /open timeout/);
});

await run("connection error: immediate failure", async () => {
  const ws = new FakeSocket();
  const p = connectCdp(ws, options);
  ws.dispatchEvent(new Event("error"));
  await assert.rejects(p, /connection failed/);
});

await run("normal response: resolves and dispatches notifications", async () => {
  const ws = new FakeSocket();
  const p = connectCdp(ws, options);
  ws.open();
  const channel = await p;
  let notification = null;
  channel.on((msg) => { notification = msg.method; });
  ws.message({ method: "Runtime.consoleAPICalled" });
  assert.equal(notification, "Runtime.consoleAPICalled");
  const pending = channel.send("Page.enable");
  const [request] = ws.sent;
  assert.equal(request.method, "Page.enable");
  ws.message({ id: request.id, result: { ok: true } });
  assert.deepEqual(await pending, { ok: true });
  channel.close();
});

await run("command never responds: bounded timeout", async () => {
  const ws = new FakeSocket();
  const p = connectCdp(ws, options);
  ws.open();
  const channel = await p;
  await assert.rejects(channel.send("Page.captureScreenshot"), /command timeout: Page.captureScreenshot/);
  channel.close();
});

await run("socket close rejects outstanding commands", async () => {
  const ws = new FakeSocket();
  const p = connectCdp(ws, options);
  ws.open();
  const channel = await p;
  const first = channel.send("Page.enable");
  const second = channel.send("Runtime.enable");
  ws.close();
  await assert.rejects(first, /WebSocket closed/);
  await assert.rejects(second, /WebSocket closed/);
  await assert.rejects(channel.send("Late.call"), /not open/);
});

await run("synchronous send exception rejects and cleans up", async () => {
  const ws = new FakeSocket();
  const p = connectCdp(ws, options);
  ws.open();
  const channel = await p;
  ws.throwSend = true;
  await assert.rejects(channel.send("Page.enable"), /synthetic send failure/);
  channel.close();
});

console.log("CDP CHANNEL TESTS OK");
