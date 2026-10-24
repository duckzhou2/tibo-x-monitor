// Dispatch only. All X, translation, email and state work stays in Actions.
export async function dispatch(env, fetcher = fetch) {
  if (env.ENABLED !== "true") return "DISABLED";
  if (!env.GITHUB_DISPATCH_TOKEN?.trim()) throw new Error("DISPATCH_TOKEN_MISSING");
  let response;
  try {
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
        redirect: "error",
        signal: AbortSignal.timeout(10000),
      },
    );
  } catch {
    throw new Error("DISPATCH_NETWORK_OR_TIMEOUT");
  }
  if (![200, 204].includes(response.status)) throw new Error(`DISPATCH_HTTP_${response.status}`);
  return "DISPATCH_ACCEPTED";
}

export default {
  async scheduled(controller, env) {
    const status = await dispatch(env);
    console.log(JSON.stringify({
      status,
      scheduled_at: controller.scheduledTime == null ? null : new Date(controller.scheduledTime).toISOString(),
      observed_at: new Date().toISOString(),
    }));
  },
};
