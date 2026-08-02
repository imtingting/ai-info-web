"use strict";

const http = require("http");
const { main } = require("./index");

const MAX_BODY_BYTES = 16384;
const port = Number(process.env.PORT || 9000);

const server = http.createServer((request, response) => {
  const chunks = [];
  let bodyBytes = 0;
  let bodyTooLarge = false;

  request.on("data", (chunk) => {
    bodyBytes += chunk.length;
    if (bodyBytes > MAX_BODY_BYTES) {
      bodyTooLarge = true;
      return;
    }
    chunks.push(chunk);
  });
  request.on("error", () => sendJson(response, 400, { error: "invalid_request" }));
  request.on("end", async () => {
    if (bodyTooLarge) {
      sendJson(response, 413, { error: "request_too_large" });
      return;
    }
    const event = {
      httpMethod: request.method,
      headers: request.headers,
      body: Buffer.concat(chunks).toString("utf8"),
      requestContext: { sourceIp: request.socket.remoteAddress || "" }
    };
    try {
      const result = await main(event);
      response.writeHead(result.statusCode || 500, result.headers || { "Content-Type": "application/json; charset=utf-8" });
      response.end(result.body || "");
    } catch (error) {
      console.log(JSON.stringify({ event: "chat_unhandled_error", error: error && error.code || "internal_error" }));
      sendJson(response, 500, { error: "internal_error" });
    }
  });
});

server.listen(port, "0.0.0.0", () => {
  console.log(JSON.stringify({ event: "chat_server_started", port }));
});

function sendJson(response, statusCode, body) {
  if (response.writableEnded) return;
  response.writeHead(statusCode, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}
