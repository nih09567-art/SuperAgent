const http = require("http");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");

const root = __dirname;
const webRoot = path.join(root, "web");

const send = (response, status, body, contentType = "application/json; charset=utf-8", headers = {}) => {
  response.writeHead(status, { "Content-Type": contentType, ...headers });
  response.end(body);
};

const readWorkflow = (workflowId) => {
  const separator = workflowId.indexOf(":");
  if (separator < 1) return null;
  const userId = workflowId.slice(0, separator);
  const fileId = workflowId.slice(separator + 1);
  const filePath = path.join(root, "store", "workflows", userId, `${fileId}.json`);
  if (!fs.existsSync(filePath)) return null;
  return fs.readFileSync(filePath, "utf8");
};

http.createServer((request, response) => {
  const requestUrl = new URL(request.url, "http://127.0.0.1:8001");

  if (requestUrl.pathname === "/api/workflows") {
    const userId = requestUrl.searchParams.get("user_id") || "admin";
    const page = Number(requestUrl.searchParams.get("page") || 1);
    const pageSize = Number(requestUrl.searchParams.get("page_size") || 5);
    const match = (requestUrl.searchParams.get("match") || "").toLowerCase();
    const directory = path.join(root, "store", "workflows", userId);
    let items = fs.existsSync(directory)
      ? fs.readdirSync(directory)
        .filter((name) => name.endsWith(".json"))
        .map((name) => JSON.parse(fs.readFileSync(path.join(directory, name), "utf8")))
      : [];
    items = items
      .filter((item) => !match || JSON.stringify(item).toLowerCase().includes(match))
      .sort((left, right) => Date.parse(right.updated_at || 0) - Date.parse(left.updated_at || 0));
    const total = items.length;
    const totalPages = total ? Math.ceil(total / pageSize) : 0;
    const paged = items.slice((page - 1) * pageSize, page * pageSize);
    send(response, 200, JSON.stringify(paged), undefined, {
      "X-Total-Count": String(total),
      "X-Total-Pages": String(totalPages),
    });
    return;
  }

  const detailMatch = requestUrl.pathname.match(/^\/api\/workflows\/([^/]+)$/);
  if (detailMatch) {
    const workflow = readWorkflow(decodeURIComponent(detailMatch[1]));
    send(response, workflow ? 200 : 404, workflow || JSON.stringify({ detail: "not found" }));
    return;
  }

  if (requestUrl.pathname === "/api/tasks") {
    send(response, 200, "[]");
    return;
  }

  if (requestUrl.pathname === "/api/health/ready") {
    send(response, 200, JSON.stringify({ ready: true, components: {} }));
    return;
  }

  if (requestUrl.pathname.startsWith("/api/")) {
    send(response, 404, JSON.stringify({ detail: "preview route not implemented" }));
    return;
  }

  const relativePath = requestUrl.pathname === "/"
    ? "index.html"
    : requestUrl.pathname.replace(/^\/static\//, "").replace(/^\//, "");
  const filePath = path.resolve(webRoot, relativePath);
  if (!filePath.startsWith(webRoot) || !fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    send(response, 404, "Not found", "text/plain; charset=utf-8");
    return;
  }
  const extension = path.extname(filePath);
  const contentTypes = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
  };
  send(response, 200, fs.readFileSync(filePath), contentTypes[extension] || "application/octet-stream");
}).listen(8001, "127.0.0.1");
