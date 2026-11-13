// FIXME: remove after S1 validation (B92).
// This intentionally contains no user data and is only deployed by an explicit spike flag.
exports.handler = awslambda.streamifyResponse(async (_event, rawStream) => {
  const stream = awslambda.HttpResponseStream.from(rawStream, {
    statusCode: 200,
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform' },
  });
  let seq = 0;
  const emit = (type, payload) => stream.write(`id: ${seq++}\nevent: ${type}\ndata: ${JSON.stringify({ type, ...payload })}\n\n`);
  emit('surface_begin', { surface_id: 'spike', revision: 1, content_type: 'application/vnd.cei.ui+json', protocol: 'cei-ui', version: '1.2', root_id: 'root' });
  emit('component_add', { parent_id: null, component: { id: 'root', type: 'Column', children: [] } });
  emit('component_add', { parent_id: 'root', component: { id: 'text', type: 'Markdown', content: '' } });
  emit('text_delta', { component_id: 'text', field: 'content', delta: 'Streaming started.' });
  for (let i = 0; i < 37; i++) {
    await new Promise((resolve) => setTimeout(resolve, 17000));
    stream.write(': heartbeat\n\n');
  }
  emit('text_delta', { component_id: 'text', field: 'content', delta: ' Streaming finished.' });
  emit('surface_end', { surface_id: 'spike', revision: 1, root_id: 'root', final: true });
  stream.end();
});
