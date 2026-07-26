export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v == null ? "—" : `${(v * 100).toFixed(digits)}%`;

export const fmtDollars = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `$${v.toFixed(digits)}`;

export const fmtInt = (v: number | null | undefined) =>
  v == null ? "—" : Intl.NumberFormat("en-US").format(v);

export const fmtRelative = (iso: string | Date | null | undefined) => {
  if (!iso) return "—";
  const d = typeof iso === "string" ? new Date(iso) : iso;
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86_400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86_400)}d ago`;
};
