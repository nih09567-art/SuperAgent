const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createLatestRequestGuard,
} = require("../web/resource-request-guard.js");

const deferred = () => {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
};

test("a stale user request cannot overwrite a newer result", async () => {
  let currentUserId = "user-a";
  const guard = createLatestRequestGuard(() => currentUserId);
  const firstResponse = deferred();
  const secondResponse = deferred();
  const rendered = [];

  const load = async (userId, response) => {
    currentUserId = userId;
    const request = guard.begin(userId);
    const value = await response.promise;
    if (guard.isCurrent(request)) rendered.push(value);
  };

  const firstLoad = load("user-a", firstResponse);
  const secondLoad = load("user-b", secondResponse);

  secondResponse.resolve("user-b-result");
  await secondLoad;
  firstResponse.resolve("user-a-result");
  await firstLoad;

  assert.deepEqual(rendered, ["user-b-result"]);
});

test("changing the current user invalidates an in-flight request", async () => {
  let currentUserId = "user-a";
  const guard = createLatestRequestGuard(() => currentUserId);
  const response = deferred();
  const request = guard.begin(currentUserId);

  currentUserId = "user-b";
  response.resolve("user-a-result");
  await response.promise;

  assert.equal(guard.isCurrent(request), false);
});
