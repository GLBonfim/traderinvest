// The only two data entry points of the UI:
// - DemoSource: static DEMO files shipped with the interface (Overview);
// - ApiSource: the two read-only market endpoints of this same server (ADR-0028), whose
//   responses must be marked LOCAL. Nothing here contacts any other origin.

const DEMO_BASE = new URL("../demo/", import.meta.url);

export class DataSourceError extends Error {}

export class ApiError extends DataSourceError {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export class DemoSource {
  constructor() {
    this.kind = "DEMO";
  }

  async load(name) {
    if (!/^[a-z0-9-]+$/.test(name)) {
      throw new DataSourceError(`Invalid dataset name: ${name}`);
    }
    let res;
    try {
      res = await fetch(new URL(`${name}.json`, DEMO_BASE), {
        method: "GET",
        credentials: "same-origin",
        cache: "no-cache",
      });
    } catch {
      throw new DataSourceError("The interface could not reach its own files.");
    }
    if (!res.ok) {
      throw new DataSourceError(`Demonstration dataset "${name}" is unavailable (HTTP ${res.status}).`);
    }
    const body = await res.json();
    // Refuse anything not explicitly marked as demonstration data.
    if (!body || !body.meta || body.meta.source !== "DEMO") {
      throw new DataSourceError(`Dataset "${name}" is not marked as DEMO and was not displayed.`);
    }
    return body;
  }
}

export const source = new DemoSource();

export const RANGES = Object.freeze(["3m", "6m", "1y", "2y", "5y"]);
export const SLICES = Object.freeze(["full", "early", "late"]);
export const CURVE_KINDS = Object.freeze(["net", "gross"]);

// The only API paths the UI may call (ADR-0028, ADR-0029), each with its allowed parameters.
const API = Object.freeze({
  "market-summary": { path: "/api/v1/market/summary", params: {} },
  "market-series": { path: "/api/v1/market/series", params: { range: RANGES } },
  "research-strategies": { path: "/api/v1/research/strategies", params: {} },
  "research-backtests": { path: "/api/v1/research/backtests", params: { slice: SLICES } },
  "research-curves": { path: "/api/v1/research/curves", params: { slice: SLICES, kind: CURVE_KINDS } },
});

// Same-origin URL of an allowed dataset; unknown names, parameters or values are refused.
export function apiUrl(name, params = {}) {
  const spec = API[name];
  if (!spec) throw new DataSourceError(`Unknown dataset: ${name}`);
  const path = spec.path;
  const url = new URL(path, window.location.origin); // same origin only
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined) continue;
    if (!spec.params[k] || !spec.params[k].includes(v)) {
      throw new DataSourceError(`Invalid parameter for ${name}: ${k}=${v}`);
    }
    url.searchParams.set(k, v);
  }
  return url;
}

export class ApiSource {
  constructor() {
    this.kind = "LOCAL";
  }

  async load(name, params = {}) {
    const url = apiUrl(name, params);
    let res;
    try {
      res = await fetch(url, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
    } catch {
      throw new ApiError("The local research server could not be reached.", 0);
    }
    if (res.status === 503) throw new ApiError("The local research database is unavailable.", 503);
    if (res.status === 404) throw new ApiError("No stored data is available for this request.", 404);
    if (!res.ok) throw new ApiError(`The local research server answered HTTP ${res.status}.`, res.status);
    const body = await res.json();
    // Refuse anything not explicitly marked as stored local research data.
    if (!body || !body.meta || body.meta.source !== "LOCAL") {
      throw new DataSourceError(`Response "${name}" is not marked as LOCAL and was not displayed.`);
    }
    return body;
  }
}

export const api = new ApiSource();
