import { createReadStream, statSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { extname, join, normalize } from "node:path";
import { pipeline } from "node:stream";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("./dist", import.meta.url));
const port = Number.parseInt(process.env.PORT ?? "3000", 10);
const apiUpstream = parseUpstream(process.env.API_UPSTREAM_URL);
// With an upstream, the browser calls /api on this origin and this server forwards the call with
// the signed-in user's access token. Without one, the browser calls API_BASE_URL directly (local only).
const apiBaseUrl = apiUpstream ? "" : (process.env.API_BASE_URL ?? "http://localhost:3000");
const UPSTREAM_TIMEOUT_MS = 120_000;
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-length",
  "content-type",
  "if-none-match",
];
const DROPPED_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-connection",
  "set-cookie",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function parseUpstream(value) {
  if (!value || !value.trim()) {
    return null;
  }
  const url = new URL(value.trim());
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(local && url.protocol === "http:")) {
    throw new Error("API_UPSTREAM_URL must use https (http is allowed only for localhost).");
  }
  if (url.pathname !== "/" || url.search || url.hash || url.username || url.password) {
    throw new Error("API_UPSTREAM_URL must be an origin such as https://api.example.net.");
  }
  return url;
}

function sendJson(response, statusCode, detail) {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  response.writeHead(statusCode, { "Content-Type": contentTypes[".json"], "Cache-Control": "no-store" });
  response.end(JSON.stringify({ detail }));
}

function proxyApi(request, response, pathAndQuery) {
  const headers = {};
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers[name];
    if (typeof value === "string") {
      headers[name] = value;
    }
  }
  // App Service Authentication injects this header and strips client-supplied X-MS-* headers.
  // The API validates the token again, so a forged value cannot grant access.
  const token = request.headers["x-ms-token-aad-access-token"];
  if (typeof token === "string" && token) {
    headers.authorization = `Bearer ${token}`;
  }
  const send = apiUpstream.protocol === "https:" ? httpsRequest : httpRequest;
  const upstream = send(
    {
      protocol: apiUpstream.protocol,
      hostname: apiUpstream.hostname,
      port: apiUpstream.port || undefined,
      method: request.method,
      path: pathAndQuery,
      headers,
      timeout: UPSTREAM_TIMEOUT_MS,
    },
    (upstreamResponse) => {
      const responseHeaders = {};
      for (const [name, value] of Object.entries(upstreamResponse.headers)) {
        if (value !== undefined && !DROPPED_RESPONSE_HEADERS.has(name)) {
          responseHeaders[name] = value;
        }
      }
      response.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders);
      pipeline(upstreamResponse, response, (error) => {
        if (error && !response.destroyed) {
          response.destroy(error);
        }
      });
    },
  );
  upstream.on("timeout", () => upstream.destroy(new Error("upstream timeout")));
  upstream.on("error", () => sendJson(response, 502, "The pricing API is not reachable."));
  pipeline(request, upstream, () => undefined);
}

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function isFile(path) {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

function sendFile(filePath, response) {
  response.writeHead(200, { "Content-Type": contentTypes[extname(filePath)] ?? "application/octet-stream" });
  pipeline(createReadStream(filePath), response, (error) => {
    if (error && !response.destroyed) {
      response.destroy(error);
    }
  });
}

createServer((request, response) => {
  let path;
  try {
    path = new URL(request.url ?? "/", "http://localhost").pathname;
  } catch {
    response.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Invalid request URL.");
    return;
  }
  if (apiUpstream && (path === "/api" || path.startsWith("/api/"))) {
    const target = new URL(request.url ?? "/", "http://localhost");
    proxyApi(request, response, `${target.pathname}${target.search}`);
    return;
  }
  if (path === "/config.json") {
    response.writeHead(200, { "Content-Type": contentTypes[".json"], "Cache-Control": "no-store" });
    response.end(JSON.stringify({ apiBaseUrl }));
    return;
  }

  const requestedPath = path === "/"
    ? "index.html"
    : normalize(path).replace(/^[/\\]+/, "").replace(/^(\.\.[/\\])+/, "");
  const candidate = join(root, requestedPath);
  const fallback = join(root, "index.html");
  const filePath = isFile(candidate) ? candidate : fallback;
  if (!isFile(filePath)) {
    response.writeHead(503, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("The web application has not been built.");
    return;
  }
  sendFile(filePath, response);
}).listen(port, "0.0.0.0");
