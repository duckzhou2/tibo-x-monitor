// Dispatch only. All X, translation, email and state work stays in Actions.
function failure(status, diagnostic) {
  return Object.assign(new Error(status), { diagnostic: { ...diagnostic, status } });
}

export async function dispatch(env, fetcher = fetch) {
  const started = Date.now();
  const diagnostic = { elapsed_ms: 0, http_status: null, exception_name: null, aborted: false };
  if (env.ENABLED !== "true") return { ...diagnostic, status: "DISABLED" };
  if (!env.GITHUB_DISPATCH_TOKEN?.trim()) throw failure("DISPATCH_TOKEN_MISSING", diagnostic);
  let response;
  let signal;
  try {
    signal = AbortSignal.timeout(10000);
    response = await fetcher(
      "https://api.github.com/repos/duckzhou2/tibo-x-monitor/actions/workflows/monitor.yml/dispatches",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "tibo-monitor-scheduler",
          "X-GitHub-Api-Version": "2026-03-10",
        },
        body: JSON.stringify({ ref: "main", inputs: { mode: "monitor" } }),
        // Workers rejects "error" before sending. Never follow credentialed redirects.
        redirect: "manual",
        signal,
      },
    );
  } catch (error) {
    diagnostic.elapsed_ms = Date.now() - started;
    diagnostic.aborted = signal?.aborted ?? false;
    diagnostic.exception_name = ["Error", "TypeError", "ReferenceError", "RangeError", "AbortError", "TimeoutError"].includes(error?.name) ? error.name : "OtherError";
    const timeout = signal?.aborted || error?.name === "TimeoutError" || error?.name === "AbortError";
    // Inspect only for known runtime signatures; never emit upstream messages or stacks.
    const runtimeError = ["ReferenceError", "RangeError"].includes(error?.name)
      || (error?.name === "TypeError" && /(?:unsupported|invalid).*redirect|illegal invocation|(?:AbortSignal\.timeout|fetch).*not a function/i.test(String(error?.message ?? "")));
    throw failure(timeout ? "REQUEST_TIMEOUT" : runtimeError ? "FETCH_RUNTIME_ERROR" : "NETWORK_ERROR", diagnostic);
  }
  diagnostic.elapsed_ms = Date.now() - started;
  diagnostic.http_status = response.status;
  if (![200, 204].includes(response.status)) {
    const status = response.status >= 500 ? "DISPATCH_HTTP_5XX"
      : response.status >= 400 ? "DISPATCH_HTTP_4XX" : "DISPATCH_HTTP_UNEXPECTED";
    throw failure(status, diagnostic);
  }
  return { ...diagnostic, status: "DISPATCH_ACCEPTED" };
}

export default {
  async scheduled(controller, env) {
    const timing = {
      scheduled_at: controller.scheduledTime == null ? null : new Date(controller.scheduledTime).toISOString(),
      observed_at: new Date().toISOString(),
    };
    try {
      const result = await dispatch(env);
      console.log(JSON.stringify({ ...timing, ...result }));
    } catch (error) {
      console.log(JSON.stringify({ ...timing, ...error.diagnostic }));
      throw error;
    }
  },
};
