// Proxy boundary tests for server.mjs. Uses only Node built-ins: node --test server.test.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { once } from "node:events";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";

const serverPath = fileURLToPath(new URL("./server.mjs", import.meta.url));
let upstream;
let web;
let webPort;
let seen;

async function freePort() {
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const { port } = probe.address();
  probe.close();
  await once(probe, "close");
  return port;
}

async function waitForServer(port) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      await fetch(`http://127.0.0.1:${port}/config.json`);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error("server.mjs did not start");
}

before(async () => {
  upstream = createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      seen = { method: request.method, url: request.url, headers: request.headers, body: Buffer.concat(chunks).toString() };
      response.writeHead(201, { "content-type": "application/json", "set-cookie": "leak=1", "x-upstream": "yes" });
      response.end(JSON.stringify({ ok: true }));
    });
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  webPort = await freePort();
  web = spawn(process.execPath, [serverPath], {
    env: { ...process.env, PORT: String(webPort), API_UPSTREAM_URL: `http://localhost:${upstream.address().port}` },
    stdio: "ignore",
  });
  await waitForServer(webPort);
});

after(() => {
  web?.kill();
  upstream?.close();
});

test("config tells the browser to call its own origin", async () => {
  const response = await fetch(`http://127.0.0.1:${webPort}/config.json`);
  assert.deepEqual(await response.json(), { apiBaseUrl: "" });
});

test("forwards only the platform token and allowlisted headers", async () => {
  const response = await fetch(`http://127.0.0.1:${webPort}/api/intakes?x=1`, {
    method: "POST",
    headers: {
      authorization: "Bearer forged-by-caller",
      "x-ms-client-principal": "forged",
      "x-ms-client-principal-idp": "aad",
      "x-ms-token-aad-access-token": "platform-token",
      cookie: "AppServiceAuthSession=secret",
      "x-forwarded-for": "203.0.113.9",
      "content-type": "text/plain",
    },
    body: "hello",
  });
  assert.equal(response.status, 201);
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("x-upstream"), "yes");
  assert.equal(seen.method, "POST");
  assert.equal(seen.url, "/api/intakes?x=1");
  assert.equal(seen.body, "hello");
  assert.equal(seen.headers.authorization, "Bearer platform-token");
  assert.equal(seen.headers["content-length"], "5");
  for (const name of ["x-ms-client-principal", "x-ms-client-principal-idp", "x-ms-token-aad-access-token", "cookie", "x-forwarded-for"]) {
    assert.equal(seen.headers[name], undefined, name);
  }
});

test("never forwards a caller-supplied Authorization header", async () => {
  await fetch(`http://127.0.0.1:${webPort}/api/me`, { headers: { authorization: "Bearer forged-by-caller" } });
  assert.equal(seen.headers.authorization, undefined);
});

test("does not proxy paths outside /api", async () => {
  seen = undefined;
  await fetch(`http://127.0.0.1:${webPort}/apix`);
  await fetch(`http://127.0.0.1:${webPort}/`);
  assert.equal(seen, undefined);
});

test("rejects a non-https upstream other than localhost", async () => {
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, PORT: String(await freePort()), API_UPSTREAM_URL: "http://api.example.net" },
    stdio: "ignore",
  });
  const [code] = await once(child, "exit");
  assert.notEqual(code, 0);
});
