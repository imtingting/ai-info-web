"use strict";

const crypto = require("crypto");
const https = require("https");
const cloudbase = require("@cloudbase/node-sdk");

const MAX_BODY_BYTES = 16384;
const CATALOG_CACHE_MS = 5 * 60 * 1000;
let catalogCache = { fetchedAt: 0, products: new Map() };

exports.main = async (event) => {
  const origin = header(event, "origin");
  const headers = corsHeaders(origin);
  const method = String(event.httpMethod || event.method || "GET").toUpperCase();
  if (method === "OPTIONS") return response(204, {}, headers);
  if (method !== "POST") return response(405, { error: "method_not_allowed" }, headers);

  let payload;
  try {
    const raw = typeof event.body === "string" ? event.body : JSON.stringify(event.body || {});
    if (Buffer.byteLength(raw, "utf8") > MAX_BODY_BYTES) throw new Error("body_too_large");
    payload = JSON.parse(raw);
  } catch (_) {
    return response(400, { error: "invalid_request" }, headers);
  }
  if (!payload || typeof payload.product_slug !== "string" || typeof payload.message !== "string" || !payload.message.trim()) {
    return response(400, { error: "invalid_request" }, headers);
  }
  if (payload.message.length > numberEnv("CHAT_MAX_MESSAGE_CHARACTERS", 1000)) {
    return response(400, { error: "message_too_long" }, headers);
  }

  let product;
  try {
    product = (await publishedProducts()).get(payload.product_slug);
  } catch (error) {
    audit("catalog_unavailable", { error: error.code || "fetch_failed" });
    return response(502, { error: "catalog_unavailable" }, headers);
  }
  if (!product) return response(404, { error: "unknown_product" }, headers);

  const decision = await reserveQuota(ipHash(event));
  if (decision) return response(decision === "rate_limited" ? 429 : 503, { error: decision }, headers);
  try {
    const reply = await askDeepSeek(product, payload.message.trim());
    audit("chat_ok", { product: product.slug, message_chars: payload.message.length, reply_chars: reply.length });
    return response(200, { reply }, headers);
  } catch (error) {
    audit("chat_failed", { product: product.slug, message_chars: payload.message.length, error: error.code || "provider_failed" });
    return response(502, { error: "model_unavailable" }, headers);
  }
};

async function publishedProducts() {
  if (Date.now() - catalogCache.fetchedAt < CATALOG_CACHE_MS) return catalogCache.products;
  const catalogUrl = required("CHAT_CATALOG_URL");
  const data = JSON.parse(await getHttps(catalogUrl));
  const products = new Map();
  for (const item of data.products || []) {
    if (!item || typeof item.slug !== "string") continue;
    const context = item.chat_context && typeof item.chat_context === "object" ? item.chat_context : {};
    products.set(item.slug, {
      slug: item.slug,
      name: String(item.name || ""),
      summary: String(item.summary || ""),
      readme_excerpt: String(context.readme_excerpt || "").slice(0, 2000),
      links: Array.isArray(context.links) ? context.links.filter((url) => typeof url === "string").slice(0, 4) : []
    });
  }
  catalogCache = { fetchedAt: Date.now(), products };
  return products;
}

async function reserveQuota(clientHash) {
  const app = cloudbase.init({});
  const db = app.database();
  const collection = db.collection("ai_info_chat_usage");
  const now = new Date();
  const hour = now.toISOString().slice(0, 13);
  const month = now.toISOString().slice(0, 7);
  const ipKey = `ip:${hour}:${clientHash}`;
  const monthKey = `month:${month}`;
  const maximumRequests = numberEnv("CHAT_MAX_REQUESTS_PER_IP_PER_HOUR", 20);
  const monthlyBudget = numberEnv("CHAT_MONTHLY_BUDGET_CNY", 10);
  const reservation = numberEnv("CHAT_RESERVATION_CNY", 0.03);
  let rejection = null;
  await db.runTransaction(async (transaction) => {
    const [ipRecord, monthRecord] = await Promise.all([getDocument(transaction, collection, ipKey), getDocument(transaction, collection, monthKey)]);
    const requests = ipRecord ? Number(ipRecord.request_count || 0) : 0;
    const reserved = monthRecord ? Number(monthRecord.reserved_cny || 0) : 0;
    if (requests >= maximumRequests) { rejection = "rate_limited"; return; }
    if (reserved + reservation > monthlyBudget) { rejection = "budget_exhausted"; return; }
    await Promise.all([
      transaction.collection("ai_info_chat_usage").doc(ipKey).set({ request_count: requests + 1, updated_at: now.toISOString() }),
      transaction.collection("ai_info_chat_usage").doc(monthKey).set({ reserved_cny: reserved + reservation, updated_at: now.toISOString() })
    ]);
  });
  return rejection;
}

async function getDocument(transaction, collection, id) {
  try {
    const result = await transaction.collection("ai_info_chat_usage").doc(id).get();
    return result && result.data ? result.data : null;
  } catch (_) { return null; }
}

async function askDeepSeek(product, message) {
  const token = required("DEEPSEEK_API_KEY");
  const prompt = [
    "仅依据以下项目资料回答；资料不足时明确说明未知，不要编造。",
    `项目名称：${product.name}`,
    `项目摘要：${product.summary}`,
    `README 截要：${product.readme_excerpt || "未提供"}`,
    `项目链接：\n${product.links.map((url) => `- ${url}`).join("\n")}`,
    `用户问题：${message}`
  ].join("\n");
  const raw = await postHttps("https://api.deepseek.com/chat/completions", {
    model: process.env.DEEPSEEK_MODEL || "deepseek-chat",
    messages: [{ role: "system", content: "You answer product questions in factual Simplified Chinese." }, { role: "user", content: prompt }],
    temperature: 0.2,
    max_tokens: numberEnv("CHAT_MAX_OUTPUT_TOKENS", 400)
  }, { Authorization: `Bearer ${token}` });
  const text = raw && raw.choices && raw.choices[0] && raw.choices[0].message && raw.choices[0].message.content;
  if (typeof text !== "string" || !text.trim()) throw Object.assign(new Error("empty_reply"), { code: "empty_reply" });
  return text.trim().slice(0, 4000);
}

function ipHash(event) {
  const raw = header(event, "x-forwarded-for").split(",")[0].trim() || String(event.requestContext && event.requestContext.sourceIp || "unknown");
  return crypto.createHash("sha256").update(`${required("CHAT_IP_HASH_SALT")}:${raw}`).digest("hex");
}

function corsHeaders(origin) {
  const allowed = String(process.env.CHAT_ALLOWED_ORIGINS || "").split(",").map((item) => item.trim()).filter(Boolean);
  return origin && allowed.includes(origin) ? { "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST, OPTIONS", Vary: "Origin" } : {};
}

function header(event, name) {
  const headers = event.headers || {};
  return String(headers[name] || headers[name.toLowerCase()] || headers[name.toUpperCase()] || "");
}

function response(statusCode, body, headers) {
  return { statusCode, headers: { "Content-Type": "application/json; charset=utf-8", ...headers }, body: statusCode === 204 ? "" : JSON.stringify(body) };
}

function required(name) {
  const value = process.env[name];
  if (!value) throw Object.assign(new Error(`${name} is required`), { code: "missing_configuration" });
  return value;
}

function numberEnv(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

function getHttps(url) {
  return requestHttps(url, "GET");
}

function postHttps(url, payload, extraHeaders) {
  return requestHttps(url, "POST", JSON.stringify(payload), { "Content-Type": "application/json", ...extraHeaders }).then(JSON.parse);
}

function requestHttps(url, method, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") return reject(Object.assign(new Error("https required"), { code: "invalid_url" }));
    const request = https.request(parsed, { method, headers: { ...headers, ...(body ? { "Content-Length": Buffer.byteLength(body) } : {}) }, timeout: 20000 }, (result) => {
      let data = "";
      result.setEncoding("utf8");
      result.on("data", (chunk) => { data += chunk; });
      result.on("end", () => result.statusCode >= 200 && result.statusCode < 300 ? resolve(data) : reject(Object.assign(new Error("upstream failed"), { code: "upstream_failed" })));
    });
    request.on("timeout", () => request.destroy(Object.assign(new Error("timeout"), { code: "timeout" })));
    request.on("error", reject);
    request.end(body);
  });
}

function audit(event, fields) {
  console.log(JSON.stringify({ event, ...fields }));
}
