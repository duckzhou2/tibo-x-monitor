import test from "node:test";
import assert from "node:assert/strict";
import worker, { dispatch } from "./worker.mjs";

const env = { ENABLED: "true", GITHUB_DISPATCH_TOKEN: "fixture-only" };

test("disabled configuration never dispatches", async () => {
  assert.equal(await dispatch({}, () => assert.fail("network")), "DISABLED");
});

test("enabled schedule requires a secret", async () => {
  await assert.rejects(dispatch({ ENABLED: "true" }, () => assert.fail("network")), /TOKEN_MISSING/);
});

test("request is restricted to main monitor on this repository", async () => {
  let calls = 0;
  assert.equal(await dispatch(env, async (url, request) => {
    calls++;
    assert.equal(url, "https://api.github.com/repos/duckzhou2/tibo-x-monitor/actions/workflows/monitor.yml/dispatches");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.Authorization, "Bearer fixture-only");
    assert.deepEqual(JSON.parse(request.body), { ref: "main", inputs: { mode: "monitor" } });
    assert.equal(request.redirect, "error");
    assert.ok(request.signal instanceof AbortSignal);
    return { status: 204 };
  }), "DISPATCH_ACCEPTED");
  assert.equal(calls, 1);
});

test("current API 200 and legacy 204 both confirm acceptance", async () => {
  for (const status of [200, 204]) {
    assert.equal(await dispatch(env, async () => ({ status })), "DISPATCH_ACCEPTED");
  }
});

test("HTTP errors expose status only and do not retry POST", async () => {
  for (const status of [302, 400, 401, 403, 429, 500]) {
    let calls = 0;
    await assert.rejects(dispatch(env, async () => {
      calls++;
      return { status, text() { assert.fail("must not read response body"); } };
    }), new RegExp(`DISPATCH_HTTP_${status}`));
    assert.equal(calls, 1);
  }
});

test("network errors are sanitized", async () => {
  await assert.rejects(dispatch(env, async () => { throw new Error("sensitive upstream details"); }), { message: "DISPATCH_NETWORK_OR_TIMEOUT" });
});

test("scheduled handler awaits dispatch failure", async () => {
  await assert.rejects(worker.scheduled({}, { ENABLED: "true" }), /TOKEN_MISSING/);
});
