// Loopback HTTP to a Unix-domain socket; works in containers without a network.
const http = require('node:http');
const socketPath = '/run/bench-gateway/gateway.sock';
http.createServer((req, res) => {
  const upstream = http.request({socketPath, path: req.url, method: req.method,
      headers: req.headers}, incoming => {
    res.writeHead(incoming.statusCode, incoming.headers);
    incoming.pipe(res);
  });
  upstream.on('error', () => {res.writeHead(502); res.end('Local gateway unavailable');});
  req.pipe(upstream);
  req.on('aborted', () => upstream.destroy());
  res.on('close', () => { if (!res.writableEnded) upstream.destroy(); });
}).listen(8765, '127.0.0.1');
