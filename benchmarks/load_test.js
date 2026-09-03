import http from "k6/http";
import { check } from "k6";

// Reads one positive integer environment value with a stable fallback.
function integerSetting(name, fallback) {
  const raw = __ENV[name];
  const value = raw === undefined ? fallback : Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

// Requires a virtual key without including it in source code or result files.
if (!__ENV.GATEWAY_KEY) {
  throw new Error("Set GATEWAY_KEY before running k6.");
}

// Selects safe load dimensions that can be raised deliberately on each run.
const peakVus = integerSetting("PEAK_VUS", 25);
const stageDuration = __ENV.STAGE_DURATION || "20s";
const p95LimitMs = integerSetting("P95_LIMIT_MS", 2000);
const errorLimit = Number.parseFloat(__ENV.ERROR_RATE_LIMIT || "0.01");

// Ramps through several concurrency levels so degradation becomes visible.
export const options = {
  discardResponseBodies: false,
  scenarios: {
    gateway_load: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: stageDuration, target: Math.max(1, Math.round(peakVus * 0.25)) },
        { duration: stageDuration, target: Math.max(1, Math.round(peakVus * 0.50)) },
        { duration: stageDuration, target: Math.max(1, Math.round(peakVus * 0.75)) },
        { duration: stageDuration, target: peakVus },
        { duration: "5s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    http_req_failed: [`rate<${errorLimit}`],
    http_req_duration: [`p(95)<${p95LimitMs}`],
  },
  summaryTrendStats: ["min", "med", "p(90)", "p(95)", "p(99)", "max", "avg"],
};

// Sends one complete streaming or non-streaming request per virtual user iteration.
export default function () {
  const streamRequested = (__ENV.STREAM || "false").toLowerCase() === "true";
  const gatewayUrl =
    __ENV.GATEWAY_URL || "http://127.0.0.1:8000/v1/chat/completions";
  const model = __ENV.MODEL || "fireworks/deepseek-v4-flash";

  // Explicit cache opt-out measures gateway and provider throughput, not cache speed.
  const body = JSON.stringify({
    model,
    messages: [{ role: "user", content: "Reply with exactly: load test" }],
    temperature: 0.2,
    cache: false,
    max_tokens: integerSetting("MAX_TOKENS", 32),
    stream: streamRequested,
  });

  // Authenticates exactly like a real OpenAI-compatible client application.
  const response = http.post(gatewayUrl, body, {
    headers: {
      Authorization: `Bearer ${__ENV.GATEWAY_KEY}`,
      "Content-Type": "application/json",
    },
    tags: { response_mode: streamRequested ? "streaming" : "complete" },
    timeout: __ENV.REQUEST_TIMEOUT || "120s",
  });

  // Counts every non-200 provider or gateway response in k6's check metric.
  check(response, {
    "status is 200": (result) => result.status === 200,
  });
}

// Writes one compact machine-readable summary for honest run-to-run comparison.
export function handleSummary(data) {
  const duration = data.metrics.http_req_duration?.values || {};
  const iterations = data.metrics.iterations?.values || {};
  const failures = data.metrics.http_req_failed?.values || {};
  const checks = data.metrics.checks?.values || {};
  const streamRequested = (__ENV.STREAM || "false").toLowerCase() === "true";

  // Keeps only the numbers needed to identify a defensible throughput ceiling.
  const summary = {
    benchmark: "gateway_load_test",
    response_mode: streamRequested ? "streaming" : "complete",
    configured_peak_vus: peakVus,
    requests_per_second: iterations.rate || 0,
    completed_requests: iterations.count || 0,
    error_rate: failures.rate || 0,
    check_pass_rate: checks.rate || 0,
    latency_ms: {
      p50: duration.med || 0,
      p95: duration["p(95)"] || 0,
      p99: duration["p(99)"] || 0,
      max: duration.max || 0,
    },
    thresholds: {
      p95_limit_ms: p95LimitMs,
      error_rate_limit: errorLimit,
    },
  };

  // Uses a caller-selected file so streaming and complete runs stay separate.
  const resultFile =
    __ENV.RESULT_FILE ||
    `benchmarks/results/load-${streamRequested ? "streaming" : "complete"}.json`;

  return {
    stdout: `${JSON.stringify(summary, null, 2)}\n`,
    [resultFile]: `${JSON.stringify(summary, null, 2)}\n`,
  };
}
