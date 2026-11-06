import { TRUST, type TrustLevel } from "../lib/trust";

/** The one trust label (lib/trust.ts): Certified, Checked or Unchecked, with its meaning on hover and for readers. */
export function TrustBadge({ level }: { level: TrustLevel }) {
  const t = TRUST[level];
  const cls = t.tone === "success" ? "tag tag-success" : t.tone === "info" ? "tag tag-info" : "tag";
  return <span className={cls} title={t.hint} aria-label={`${t.label}: ${t.hint}`}>{t.label}</span>;
}
