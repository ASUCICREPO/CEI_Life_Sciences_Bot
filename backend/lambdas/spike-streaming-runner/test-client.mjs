// FIXME: remove after S1 validation (B92).
// SPIKE_URL and SPIKE_ID_TOKEN are supplied via environment, never command arguments.
const { SPIKE_URL: url, SPIKE_ID_TOKEN: token } = process.env;
if (!url || !token) throw new Error('Set SPIKE_URL and SPIKE_ID_TOKEN in the environment.');
const started = Date.now();
const response = await fetch(url, { method: 'POST', headers: { Authorization: token }, signal: AbortSignal.timeout(850000) });
if (!response.ok || !response.body) throw new Error(`Streaming request failed: HTTP ${response.status}`);
const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
let buffered = '';
let expected = 0;
let heartbeats = 0;
let lastHeartbeat = started;
let ended = false;
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  // Normalize after appending: a CRLF delimiter can be split across network chunks.
  buffered = (buffered + value).replace(/\r\n/g, '\n');
  let split;
  while ((split = buffered.indexOf('\n\n')) !== -1) {
    const frame = buffered.slice(0, split);
    buffered = buffered.slice(split + 2);
    if (frame.startsWith(':')) {
      const now = Date.now();
      if (now - lastHeartbeat > 30000) throw new Error('Heartbeat arrived more than 30 seconds after the previous event.');
      lastHeartbeat = now;
      heartbeats++;
      continue;
    }
    const id = frame.match(/^id: (\d+)$/m);
    const type = frame.match(/^event: (.+)$/m);
    const data = frame.match(/^data: (.+)$/m);
    if (!id || !type || !data || Number(id[1]) !== expected++) throw new Error('Malformed or out-of-order SSE event.');
    if (expected === 1 && Date.now() - started > 30000) throw new Error('The first stream event was not delivered progressively.');
    if (JSON.parse(data[1]).type !== type[1]) throw new Error('SSE event type mismatch.');
    if (type[1] === 'surface_end') ended = true;
  }
}
if (!ended || heartbeats < 35 || Date.now() - started < 600000) throw new Error('Stream ended before the required ten-minute check.');
process.stdout.write(`PASS: ${expected} ordered events, ${heartbeats} heartbeats, ${Math.round((Date.now() - started) / 1000)} seconds.\n`);
