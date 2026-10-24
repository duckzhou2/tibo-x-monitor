import test, { mock } from "node:test";
import assert from "node:assert/strict";
import worker, { dispatch } from "./worker.mjs";

const env = { ENABLED: "true", GITHUB_DISPATCH_TOKEN: "fixture-only" };

test("disabled configuration never dispatches", async () => {
  assert.equal((await dispatch({}, () => assert.fail("network"))).status, "DISABLED");
});

test("enabled schedule requires a secret", async () => {
  await assert.rejects(dispatch({ ENABLED: "true" }, () => assert.fail("network")), /TOKEN_MISSING/);
});

test("request is restricted to main monitor on this repository", async () => {
  let calls = 0;
  const result = await dispatch(env, async (url, request) => {
    calls++;
    assert.equal(url, "https://api.github.com/repos/duckzhou2/tibo-x-monitor/actions/workflows/monitor.yml/dispatches");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.Authorization, "Bearer fixture-only");
    assert.deepEqual(JSON.parse(request.body), { ref: "main", inputs: { mode: "monitor" } });
    assert.equal(request.redirect, "manual");
    assert.equal(request.headers["User-Agent"], "tibo-monitor-scheduler");
    assert.equal(request.headers["X-GitHub-Api-Version"], "2026-03-10");
    assert.ok(request.signal instanceof AbortSignal);
    return { status: 204 };
  });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(result.http_status, 204);
  assert.ok(result.elapsed_ms >= 0);
  assert.equal(calls, 1);
});

test("current API 200 and legacy 204 both confirm acceptance", async () => {
  for (const status of [200, 204]) {
    assert.equal((await dispatch(env, async () => ({ status }))).status, "DISPATCH_ACCEPTED");
  }
});

test("HTTP errors expose status only and do not retry POST", async () => {
  for (const status of [302, 400, 401, 403, 429, 500]) {
    let calls = 0;
    await assert.rejects(dispatch(env, async () => {
      calls++;
      return { status, text() { assert.fail("must not read response body"); } };
    }), (error) => {
      assert.equal(error.message, status >= 500 ? "DISPATCH_HTTP_5XX" : status >= 400 ? "DISPATCH_HTTP_4XX" : "DISPATCH_HTTP_UNEXPECTED");
      assert.equal(error.diagnostic.http_status, status);
      return true;
    });
    assert.equal(calls, 1);
  }
});

test("network errors are sanitized", async () => {
  await assert.rejects(dispatch(env, async () => { throw new Error("sensitive upstream details"); }), { message: "NETWORK_ERROR" });
});

test("scheduled handler awaits dispatch failure", async () => {
  await assert.rejects(worker.scheduled({}, { ENABLED: "true" }), /TOKEN_MISSING/);
});

test("disabled scheduled events neither fetch nor read the token", async () => {
  const network = mock.method(globalThis, "fetch", () => assert.fail("network"));
  const logs = [];
  const log = mock.method(console, "log", (value) => logs.push(JSON.parse(value)));
  try {
    for (const ENABLED of [undefined, "false", false, "TRUE", "true "]) {
      await worker.scheduled({ scheduledTime: 1792800000000 }, {
        ENABLED,
        get GITHUB_DISPATCH_TOKEN() { assert.fail("disabled event must not read token"); },
      });
    }
    assert.equal(network.mock.callCount(), 0);
    assert.equal(logs.length, 5);
    for (const entry of logs) {
      assert.equal(entry.status, "DISABLED");
      assert.equal(entry.scheduled_at, new Date(1792800000000).toISOString());
      assert.ok(Number.isFinite(Date.parse(entry.observed_at)));
    }
  } finally { network.mock.restore(); log.mock.restore(); }
});

test("abort signal timeout is bounded and failure is sanitized", async () => {
  const timeout = mock.method(AbortSignal, "timeout", (ms) => {
    assert.equal(ms, 10000);
    return AbortSignal.abort();
  });
  try {
    await assert.rejects(dispatch(env, async (_url, request) => {
      assert.equal(request.signal.aborted, true);
      throw new Error("fixture credential must not be exposed");
    }), (error) => {
      assert.equal(error.message, "REQUEST_TIMEOUT");
      assert.equal(error.diagnostic.aborted, true);
      assert.equal(error.diagnostic.http_status, null);
      return true;
    });
  } finally { timeout.mock.restore(); }
});

test("Workers runtime regression: reject unsupported redirect mode before any HTTP", async () => {
  let calls = 0;
  const result = await dispatch(env, async (_url, request) => {
    if (request.redirect === "error") throw new TypeError("Unsupported redirect mode: error");
    assert.equal(request.redirect, "manual");
    calls++;
    return { status: 204 };
  });
  assert.equal(result.status, "DISPATCH_ACCEPTED");
  assert.equal(calls, 1);
});

test("manual redirect never follows Location or reads response body", async () => {
  let calls = 0;
  await assert.rejects(dispatch(env, async (_url, request) => {
    calls++;
    assert.equal(request.redirect, "manual");
    return {
      status: 307,
      get headers() { assert.fail("must not inspect or follow Location"); },
      text() { assert.fail("must not read body"); },
    };
  }), { message: "DISPATCH_HTTP_UNEXPECTED" });
  assert.equal(calls, 1);
});

test("known fetch runtime errors differ from network failures without exposing messages", async () => {
  for (const error of [new TypeError("Unsupported redirect mode: error"), new TypeError("Illegal invocation"), new ReferenceError("fixture-only")]) {
    await assert.rejects(dispatch(env, async () => { throw error; }), (actual) => {
      assert.equal(actual.message, "FETCH_RUNTIME_ERROR");
      assert.equal(actual.diagnostic.exception_name, error.name);
      assert.equal(actual.diagnostic.aborted, false);
      assert.equal(actual.diagnostic.http_status, null);
      assert.ok(actual.diagnostic.elapsed_ms >= 0);
      assert.equal(JSON.stringify(actual).includes(error.message), false);
      return true;
    });
  }
});

test("network TypeError is not automatically labeled a runtime argument error", async () => {
  for (const message of ["fetch failed: fixture credentials", "fetch failed at https://redirect.example.invalid", "TLS unsupported cipher"]) {
    await assert.rejects(dispatch(env, async () => { throw new TypeError(message); }), (error) => {
      assert.equal(error.message, "NETWORK_ERROR");
      assert.equal(error.diagnostic.exception_name, "TypeError");
      assert.equal(JSON.stringify(error).includes(message), false);
      return true;
    });
  }
});

test("AbortSignal construction failures are classified without starting a request", async () => {
  const timeout = mock.method(AbortSignal, "timeout", () => { throw new TypeError("AbortSignal.timeout is not a function"); });
  try {
    await assert.rejects(dispatch(env, () => assert.fail("network")), { message: "FETCH_RUNTIME_ERROR" });
  } finally { timeout.mock.restore(); }
});

test("timeout exception is visible even when signal has not updated", async () => {
  await assert.rejects(dispatch(env, async () => { throw new DOMException("fixture private text", "TimeoutError"); }), (error) => {
    assert.equal(error.message, "REQUEST_TIMEOUT");
    assert.equal(error.diagnostic.exception_name, "TimeoutError");
    assert.equal(error.diagnostic.aborted, false);
    return true;
  });
});

test("scheduled success logs timings and acceptance but no credentials", async () => {
  const network = mock.method(globalThis, "fetch", async () => ({ status: 200 }));
  const logs = [];
  const log = mock.method(console, "log", (value) => logs.push(JSON.parse(value)));
  try {
    await worker.scheduled({ scheduledTime: 1792800000000 }, env);
    assert.equal(network.mock.callCount(), 1);
    assert.equal(logs.length, 1);
    assert.equal(logs[0].status, "DISPATCH_ACCEPTED");
    assert.equal(logs[0].http_status, 200);
    assert.equal(logs[0].scheduled_at, new Date(1792800000000).toISOString());
    assert.ok(Number.isFinite(Date.parse(logs[0].observed_at)));
    assert.ok(logs[0].elapsed_ms >= 0);
    assert.equal(JSON.stringify(logs).includes("fixture-only"), false);
  } finally { network.mock.restore(); log.mock.restore(); }
});

test("scheduled failure logs safe diagnostic and still fails invocation", async () => {
  const network = mock.method(globalThis, "fetch", async () => { throw new Error("Bearer fixture-only private URL and body"); });
  const logs = [];
  const log = mock.method(console, "log", (value) => logs.push(JSON.parse(value)));
  try {
    await assert.rejects(worker.scheduled({ scheduledTime: 1792800000000 }, env), { message: "NETWORK_ERROR" });
    assert.equal(network.mock.callCount(), 1);
    assert.equal(logs.length, 1);
    assert.equal(logs[0].status, "NETWORK_ERROR");
    assert.equal(logs[0].exception_name, "Error");
    assert.equal(logs[0].http_status, null);
    assert.equal(logs[0].scheduled_at, new Date(1792800000000).toISOString());
    for (const secret of ["fixture-only", "Bearer", "private URL", "body", "stack"]) assert.equal(JSON.stringify(logs).includes(secret), false);
  } finally { network.mock.restore(); log.mock.restore(); }
});

test("untrusted exception names are reduced to a fixed safe label", async () => {
  await assert.rejects(dispatch(env, async () => { throw { name: "fixture-only credential", message: "private details" }; }), (error) => {
    assert.equal(error.diagnostic.exception_name, "OtherError");
    assert.equal(JSON.stringify(error).includes("fixture-only"), false);
    return true;
  });
});
