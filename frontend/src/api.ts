const BASE = "";  // Same origin via Vite proxy

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

export async function fetchJSON(url: string, options?: RequestInit) {
  const res = await fetchWithTimeout(BASE + url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return res.json();
}

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
export const getContactProfiles = (wxids: string[]) =>
  fetchJSON("/api/contacts/profile-batch", {
    method: "POST",
    body: JSON.stringify({ wxids }),
  });
export const getSessions = () => fetchJSON("/api/sessions");
export const refreshSessions = () => fetchJSON("/api/sessions/refresh");

// ─── Messages ────────────────────────────────────────────────────

export const getMessages = (wxid: string, limit = 50) =>
  fetchJSON(`/api/messages/${wxid}?limit=${limit}`);

export const getOlderMessages = (wxid: string, beforeTime: number, limit = 50) =>
  fetchJSON(`/api/messages/${wxid}/older?before=${beforeTime}&limit=${limit}`);

export const searchMessages = (wxid: string, keyword: string, limit = 50) =>
  fetchJSON(`/api/messages/${wxid}/query?keyword=${encodeURIComponent(keyword)}&limit=${limit}`);

// ─── Send ────────────────────────────────────────────────────────

export const sendText = (wxid: string, msg: string) =>
  fetchJSON("/api/send/text", {
    method: "POST",
    body: JSON.stringify({ wxid, msg }),
  });

export const sendImage = (wxid: string, picpath: string) =>
  fetchJSON("/api/send/image", {
    method: "POST",
    body: JSON.stringify({ wxid, picpath }),
  });

export const sendFile = (wxid: string, filepath: string) =>
  fetchJSON("/api/send/file", {
    method: "POST",
    body: JSON.stringify({ wxid, filepath }),
  });

export const sendImageUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/image-upload", { method: "POST", body: form }, 60_000);
  return res.json();
};

export const sendFileUpload = async (wxid: string, file: File) => {
  const form = new FormData();
  form.append("wxid", wxid);
  form.append("file", file);
  const res = await fetchWithTimeout("/api/send/file-upload", { method: "POST", body: form }, 60_000);
  return res.json();
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
  `/api/media/image?path=${encodeURIComponent(path)}`;

export const getGifUrl = (msgXml: string) =>
  fetchJSON("/api/media/gif-url", {
    method: "POST",
    body: JSON.stringify({ msg_xml: msgXml }),
  });

export const downloadImage = async (bytesExtraHex: string, msgXml: string, msgId?: string): Promise<string> => {
  const res = await fetchWithTimeout("/api/media/download-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
