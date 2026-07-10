const BASE = "";  // Same origin via Vite proxy
export const ACCESS_KEY_STORAGE = "wechat_web_access_key";
export const ACTIVE_AGENT_STORAGE = "wechat_web_active_agent_id";

let activeAgentIdOverride = "";

export const getAccessKey = () => window.localStorage.getItem(ACCESS_KEY_STORAGE) || "";
export const setAccessKey = (key: string) => window.localStorage.setItem(ACCESS_KEY_STORAGE, key);
export const clearAccessKey = () => window.localStorage.removeItem(ACCESS_KEY_STORAGE);
export const getActiveAgentId = () => activeAgentIdOverride || window.localStorage.getItem(ACTIVE_AGENT_STORAGE) || "";
export const setActiveAgentId = (agentId: string) => {
  activeAgentIdOverride = agentId;
  window.localStorage.setItem(ACTIVE_AGENT_STORAGE, agentId);
};
export const clearActiveAgentId = () => {
  activeAgentIdOverride = "";
  window.localStorage.removeItem(ACTIVE_AGENT_STORAGE);
};

export const authQuery = () => {
  const params = new URLSearchParams();
  const key = getAccessKey();
  const agentId = getActiveAgentId();
  if (key) params.set("key", key);
  if (agentId) params.set("agent_id", agentId);
  return params.toString();
};

function authHeaders(extra?: HeadersInit): HeadersInit {
  const key = getAccessKey();
  const agentId = getActiveAgentId();
  return {
    ...(extra || {}),
    ...(key ? { "X-Access-Key": key } : {}),
    ...(agentId ? { "X-Agent-Id": agentId } : {}),
  };
}

async function fetchWithTimeout(url: string, options?: RequestInit, timeoutMs = 30_000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: options?.signal || controller.signal,
    });
  } finally {
    window.clearTimeout(timer);
  }
}

export async function fetchJSON(url: string, options?: RequestInit, timeoutMs = 30_000) {
  const res = await fetchWithTimeout(BASE + url, {
    ...options,
    headers: authHeaders({ "Content-Type": "application/json", ...(options?.headers || {}) }),
  }, timeoutMs);
  return res.json();
}

export const loginWithKey = (key: string) =>
  fetchJSON("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ key }),
    headers: { "Content-Type": "application/json" },
  });

export const getAccounts = () => fetchJSON("/api/accounts");
export const activateAccount = (agentId: string) =>
  fetchJSON("/api/accounts/activate", {
    method: "POST",
    body: JSON.stringify({ agent_id: agentId }),
  });

// ─── Contacts & Sessions ─────────────────────────────────────────

export const getSelf = () => fetchJSON("/api/self");
export const getContacts = () => fetchJSON("/api/contacts");
export const refreshContacts = () => fetchJSON("/api/contacts/refresh");
export const getContactDetail = (wxid: string) => fetchJSON(`/api/contacts/${wxid}`);
export const getContactAvatar = (wxid: string) => fetchJSON(`/api/contacts/${wxid}/avatar`);
export const batchGetContactBrief = (wxids: string[]) =>
  fetchJSON("/api/contacts/brief-batch", {
    method: "POST",
    body: JSON.stringify({ wxids }),
  });
export const getContactProfiles = (wxids: string[], gid = "", force = false) =>
  fetchJSON("/api/contacts/profile-batch", {
    method: "POST",
    body: JSON.stringify({ wxids, gid, force }),
  });
const withAgentQuery = (path: string, agentId = "") =>
  agentId ? `${path}?agent_id=${encodeURIComponent(agentId)}` : path;

export const getSessions = (agentId = "") => fetchJSON(withAgentQuery("/api/sessions", agentId));
export const refreshSessions = (agentId = "") => fetchJSON(withAgentQuery("/api/sessions/refresh", agentId));

// ─── Messages ────────────────────────────────────────────────────

export const getMessages = (wxid: string, limit = 20) =>
  fetchJSON(`/api/messages/${wxid}?limit=${limit}`);

export const getOlderMessages = (wxid: string, beforeTime: number, limit = 100) =>
  fetchJSON(`/api/messages/${wxid}/older?before=${beforeTime}&limit=${limit}`);

export const searchMessages = (wxid: string, keyword: string, limit = 50) =>
  fetchJSON(`/api/messages/${wxid}/query?keyword=${encodeURIComponent(keyword)}&limit=${limit}`);

// ─── Send ────────────────────────────────────────────────────────

export const sendText = (wxid: string, msg: string) =>
  fetchJSON("/api/send/text", {
    method: "POST",
    body: JSON.stringify({ wxid, msg }),
  });

export const sendImage = (wxid: string, picpath: string, fileData = "") =>
  fetchJSON("/api/send/image", {
    method: "POST",
    body: JSON.stringify({ wxid, picpath, fileData }),
  });

export const sendFile = (wxid: string, filepath: string, fileData = "") =>
  fetchJSON("/api/send/file", {
    method: "POST",
    body: JSON.stringify({ wxid, filepath, fileData }),
  });

export const sendVideo = (wxid: string, videopath: string, fileData = "") =>
  fetchJSON("/api/send/video", {
    method: "POST",
    body: JSON.stringify({ wxid, videopath, fileData }),
  });

export const sendGif = (wxid: string, gifpath: string, fileData = "") =>
  fetchJSON("/api/send/gif", {
    method: "POST",
    body: JSON.stringify({ wxid, gifpath, fileData }),
  });

export const sendImageUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/image-upload", { method: "POST", body: form, headers: authHeaders() }, 60_000);
  return res.json();
};

export const sendFileUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/file-upload", { method: "POST", body: form, headers: authHeaders() }, 120_000);
  return res.json();
};

export const sendVideoUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/video-upload", { method: "POST", body: form, headers: authHeaders() }, 180_000);
  return res.json();
};

export const sendGifUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/gif-upload", { method: "POST", body: form, headers: authHeaders() }, 120_000);
  return res.json();
};

export const broadcastText = (wxids: string[], msg: string, mode = "nosrc", concurrencyLimit = 10, batchSize = 100, batchInterval = 5) =>
  fetchJSON("/api/broadcast/text", {
    method: "POST",
    body: JSON.stringify({ wxids, msg, mode, concurrency_limit: concurrencyLimit, batch_size: batchSize, batch_interval: batchInterval }),
  }, 86_400_000);

export const broadcastImageUpload = async (wxids: string[], file: File, mode = "nosrc", concurrencyLimit = 10, batchSize = 100, batchInterval = 5) => {
  const form = new FormData();
  form.append("wxids", JSON.stringify(wxids));
  form.append("mode", mode);
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  form.append("file", file);
  const res = await fetchWithTimeout("/api/broadcast/image-upload", { method: "POST", body: form, headers: authHeaders() }, 86_400_000);
  return res.json();
};

export const broadcastFileUpload = async (wxids: string[], file: File, concurrencyLimit = 10, batchSize = 100, batchInterval = 5) => {
  const form = new FormData();
  form.append("wxids", JSON.stringify(wxids));
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  form.append("file", file);
  const res = await fetchWithTimeout("/api/broadcast/file-upload", { method: "POST", body: form, headers: authHeaders() }, 86_400_000);
  return res.json();
};

export type BroadcastContentOrder = "text_first" | "attachment_first";

export const broadcastMixedUpload = async (
  wxids: string[],
  msg: string,
  images: File[],
  attachment: File | null,
  order: BroadcastContentOrder,
  mode = "nosrc",
  concurrencyLimit = 10,
  batchSize = 100,
  batchInterval = 5,
) => {
  const form = new FormData();
  form.append("wxids", JSON.stringify(wxids));
  form.append("msg", msg);
  form.append("order", order);
  form.append("mode", mode);
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  for (const image of images) form.append("images", image);
  if (attachment) form.append("attachment", attachment);
  const res = await fetchWithTimeout("/api/broadcast/mixed-upload", { method: "POST", body: form, headers: authHeaders() }, 86_400_000);
  return res.json();
};

export const multiAccountBroadcastText = (agentIds: string[], targetTypes: string[], msg: string, mode = "nosrc", concurrencyLimit = 10, batchSize = 100, batchInterval = 5) =>
  fetchJSON("/api/accounts/broadcast/text", {
    method: "POST",
    body: JSON.stringify({ agent_ids: agentIds, target_types: targetTypes, msg, mode, concurrency_limit: concurrencyLimit, batch_size: batchSize, batch_interval: batchInterval }),
  }, 86_400_000);

export const multiAccountBroadcastMixedUpload = async (
  agentIds: string[],
  targetTypes: string[],
  msg: string,
  images: File[],
  attachment: File | null,
  order: BroadcastContentOrder,
  mode = "nosrc",
  concurrencyLimit = 10,
  batchSize = 100,
  batchInterval = 5,
) => {
  const form = new FormData();
  form.append("agent_ids", JSON.stringify(agentIds));
  form.append("target_types", JSON.stringify(targetTypes));
  form.append("msg", msg);
  form.append("order", order);
  form.append("mode", mode);
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  for (const image of images) form.append("images", image);
  if (attachment) form.append("attachment", attachment);
  const res = await fetchWithTimeout("/api/accounts/broadcast/mixed-upload", { method: "POST", body: form, headers: authHeaders() }, 86_400_000);
  return res.json();
};

export const getMultiAccountBroadcastTargets = (agentIds: string[], targetTypes: string[]) =>
  fetchJSON("/api/accounts/broadcast/targets", {
    method: "POST",
    body: JSON.stringify({ agent_ids: agentIds, target_types: targetTypes }),
  });

export const multiAccountBroadcastImageUpload = async (agentIds: string[], targetTypes: string[], file: File, mode = "nosrc", concurrencyLimit = 10, batchSize = 100, batchInterval = 5) => {
  const form = new FormData();
  form.append("agent_ids", JSON.stringify(agentIds));
  form.append("target_types", JSON.stringify(targetTypes));
  form.append("mode", mode);
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  form.append("file", file);
  const res = await fetchWithTimeout("/api/accounts/broadcast/image-upload", { method: "POST", body: form, headers: authHeaders() }, 86_400_000);
  return res.json();
};

export type MultiAccountBroadcastImageProgressEvent = {
  type?: "plan" | "progress" | "done";
  [key: string]: any;
};

export const multiAccountBroadcastImageUploadStream = async (
  agentIds: string[],
  targetTypes: string[],
  file: File,
  mode = "nosrc",
  concurrencyLimit = 10,
  onProgress?: (event: MultiAccountBroadcastImageProgressEvent) => void,
  batchSize = 100,
  batchInterval = 5,
) => {
  const form = new FormData();
  form.append("agent_ids", JSON.stringify(agentIds));
  form.append("target_types", JSON.stringify(targetTypes));
  form.append("mode", mode);
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  form.append("file", file);
  const res = await fetchWithTimeout("/api/accounts/broadcast/image-upload-stream", { method: "POST", body: form, headers: authHeaders() }, 600_000);
  if (!res.body) {
    const payload = await res.json();
    onProgress?.({ type: "done", ...payload });
    return payload;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalPayload: MultiAccountBroadcastImageProgressEvent | null = null;

  const handleLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const payload = JSON.parse(trimmed) as MultiAccountBroadcastImageProgressEvent;
    onProgress?.(payload);
    if (payload.type === "done") finalPayload = payload;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) handleLine(line);
  }
  buffer += decoder.decode();
  handleLine(buffer);
  return finalPayload || {};
};

export const multiAccountBroadcastFileUploadStream = async (
  agentIds: string[],
  targetTypes: string[],
  file: File,
  concurrencyLimit = 10,
  onProgress?: (event: MultiAccountBroadcastImageProgressEvent) => void,
  batchSize = 100,
  batchInterval = 5,
) => {
  const form = new FormData();
  form.append("agent_ids", JSON.stringify(agentIds));
  form.append("target_types", JSON.stringify(targetTypes));
  form.append("concurrency_limit", String(concurrencyLimit));
  form.append("batch_size", String(batchSize));
  form.append("batch_interval", String(batchInterval));
  form.append("file", file);
  const res = await fetchWithTimeout("/api/accounts/broadcast/file-upload-stream", { method: "POST", body: form, headers: authHeaders() }, 600_000);
  if (!res.body) {
    const payload = await res.json();
    onProgress?.({ type: "done", ...payload });
    return payload;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalPayload: MultiAccountBroadcastImageProgressEvent | null = null;

  const handleLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const payload = JSON.parse(trimmed) as MultiAccountBroadcastImageProgressEvent;
    onProgress?.(payload);
    if (payload.type === "done") finalPayload = payload;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) handleLine(line);
  }
  buffer += decoder.decode();
  handleLine(buffer);
  return finalPayload || {};
};

export const revokeMsg = (msgSvrid: number, toWxid: string) =>
  fetchJSON("/api/revoke", {
    method: "POST",
    body: JSON.stringify({ msg_svrid: msgSvrid, to_wxid: toWxid }),
  });

export const markAsRead = (wxid: string) =>
  fetchJSON(`/api/mark-read/${wxid}`, { method: "POST" });

export const stickyChat = (wxid: string) =>
  fetchJSON("/api/session/sticky", {
    method: "POST",
    body: JSON.stringify({ wxid }),
  });

export const unpinChat = (wxid: string) =>
  fetchJSON("/api/session/unpin", {
    method: "POST",
    body: JSON.stringify({ wxid }),
  });

export const markSessionUnread = (wxid: string) =>
  fetchJSON("/api/session/mark-unread", {
    method: "POST",
    body: JSON.stringify({ wxid }),
  });

export const muteSession = (wxid: string) =>
  fetchJSON("/api/session/mute", {
    method: "POST",
    body: JSON.stringify({ wxid }),
  });

export const unmuteSession = (wxid: string) =>
  fetchJSON("/api/session/unmute", {
    method: "POST",
    body: JSON.stringify({ wxid }),
  });

// ─── Media ───────────────────────────────────────────────────────

export const getImageUrl = (path: string) =>
  `/api/media/image?path=${encodeURIComponent(path)}${authQuery() ? `&${authQuery()}` : ""}`;

export const getDbImageUrl = (mediaId: string) =>
  `/api/media/db-image/${encodeURIComponent(mediaId)}${authQuery() ? `?${authQuery()}` : ""}`;

export const getGifUrl = (msgXml: string) =>
  fetchJSON("/api/media/gif-url", {
    method: "POST",
    body: JSON.stringify({ msg_xml: msgXml }),
  });

export const downloadImage = async (bytesExtraHex: string, msgXml: string, msgId?: string): Promise<string> => {
  const res = await fetchWithTimeout("/api/media/download-image", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ bytes_extra_hex: bytesExtraHex, msg_xml: msgXml, msg_id: msgId || "" }),
  }, 60_000);
  if (!res.ok) return "";
  const contentType = res.headers.get("content-type") || "";
  if (contentType.startsWith("image") || contentType.includes("octet-stream")) {
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }
  return ""; // error response (JSON)
};

// ─── Group ───────────────────────────────────────────────────────

export const getGroupDetail = (gid: string) => fetchJSON(`/api/group/${gid}`);
export const getGroupMembers = (gid: string) => fetchJSON(`/api/group/${gid}/members`);
export const getGroupMemberNames = (gid: string) => fetchJSON(`/api/group/${gid}/member-names`);
export const getGroupMemberDetails = (gid: string) => fetchJSON(`/api/group/${gid}/member-details`);
