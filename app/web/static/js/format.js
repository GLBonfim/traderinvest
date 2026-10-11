// Number and date formatting shared by every page. Missing values render as an em dash,
// never as 0. Locale is fixed so the output does not depend on the viewer's machine.

export const MISSING = "—";
const LOCALE = "en-US";

function isMissing(v) {
  return v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v));
}

export function formatInteger(v) {
  if (isMissing(v)) return MISSING;
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 }).format(v);
}

export function formatNumber(v, decimals = 2) {
  if (isMissing(v)) return MISSING;
  return new Intl.NumberFormat(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(v);
}

// `v` is a fraction: 0.0123 -> "1.23%"; signed adds "+" to positive values.
export function formatPercent(v, decimals = 2, signed = false) {
  if (isMissing(v)) return MISSING;
  return new Intl.NumberFormat(LOCALE, {
    style: "percent",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(v);
}

// Calendar dates ("YYYY-MM-DD") are shown as given: no time-zone conversion can shift them.
export function formatDate(iso) {
  if (isMissing(iso) || !/^\d{4}-\d{2}-\d{2}$/.test(String(iso))) return MISSING;
  const [y, m, d] = String(iso).split("-").map(Number);
  return new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, d)));
}

// Instants are always labelled with the zone they are shown in.
export function formatDateTime(iso, timeZone = "UTC") {
  if (isMissing(iso)) return MISSING;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return MISSING;
  const s = new Intl.DateTimeFormat(LOCALE, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  }).format(t);
  return `${s} ${timeZone === "America/New_York" ? "ET" : timeZone}`;
}

// 22596500 -> "22.6M"
export function formatCompact(v) {
  if (isMissing(v)) return MISSING;
  return new Intl.NumberFormat(LOCALE, { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

// "extreme_positive" -> "Extreme positive"; engine labels are shown as words, never re-coded.
export function humanize(label) {
  if (isMissing(label) || label === "") return MISSING;
  const s = String(label).replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function formatValue(v, kind) {
  switch (kind) {
    case "integer":
      return formatInteger(v);
    case "number":
      return formatNumber(v);
    case "percent":
      return formatPercent(v);
    case "date":
      return formatDate(v);
    case "datetime":
      return formatDateTime(v);
    default:
      return isMissing(v) || v === "" ? MISSING : String(v);
  }
}
