import type { ProtocolRdvMappingEntry } from "./api";

export function resolveProtocolLogin(rdv: string, mapping: Record<string, ProtocolRdvMappingEntry>) {
  const key = rdv.trim().toLowerCase();
  const entry = mapping[key];
  const raw = String(entry?.wxid || entry?.userName || "").trim();
  const wxid = /^[0-9a-f]{32}$/i.test(raw) ? "" : raw;
  return { mode: wxid ? "old" as const : "qr" as const, wxid, entry: wxid ? entry : undefined };
}
