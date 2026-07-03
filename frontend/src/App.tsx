import { useState, useCallback, useEffect, useRef, type FormEvent } from "react";
import { useWebSocket } from "./useWebSocket";
import SessionList, { type SessionMenuAction } from "./components/SessionList";
import ChatArea from "./components/ChatArea";
import {
  activateAccount,
  batchGetContactBrief,
  broadcastImageUpload,
  broadcastText,
  clearActiveAgentId,
  clearAccessKey,
  getAccessKey,
  getAccounts,
  getContactProfiles,
  getGroupMemberNames,
  loginWithKey,
  markAsRead,
  markSessionUnread,
  multiAccountBroadcastImageUpload,
  multiAccountBroadcastText,
  muteSession,
  refreshSessions,
  setActiveAgentId,
  setAccessKey,
  stickyChat,
  unmuteSession,
  unpinChat,
} from "./api";
import type { ContactProfile, Session, ChatMessage, WSMessage, WeChatAccount } from "./types";
import { replaceWechatEmojis } from "./utils/wechatEmoji";

type ViewMode = "chats" | "contacts" | "broadcast";

interface DirectoryEntry {
  wxid: string;
  name: string;
  avatar: string;
  is_group: boolean;
  source: "friend" | "group";
}

interface BroadcastImageItem {
  id: string;
  token: string;
  label: string;
  file: File;
  preview: string;
}

type BroadcastPayloadPart =
  | { type: "text"; text: string }
  | { type: "image"; image: BroadcastImageItem };

function buildBroadcastParts(message: string, images: BroadcastImageItem[]): BroadcastPayloadPart[] {
  const parts: BroadcastPayloadPart[] = [];
  const imageByToken = new Map(images.map((image) => [image.token, image]));
  const tokenPattern = /【图片\d+】/g;
  let cursor = 0;

  for (const match of message.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    const token = match[0];
    const text = message.slice(cursor, index).trim();
    if (text) parts.push({ type: "text", text });

    const image = imageByToken.get(token);
    if (image) parts.push({ type: "image", image });
    cursor = index + token.length;
  }

  const tail = message.slice(cursor).trim();
  if (tail) parts.push({ type: "text", text: tail });
  return parts;
}

// ─── System / internal sessions to filter out ────────────────────
const FILTERED_WXIDS = new Set([
  "@placeholder_foldgroup",
  "@publicUser",
  "fmessage",
  "floatbottle",
  "medianote",
]);

function shouldFilterSession(wxid: string): boolean {
  if (!wxid) return true;
  if (FILTERED_WXIDS.has(wxid)) return true;
  if (wxid.includes("@openim")) return true;      // OpenIM
  return false;
}

// ─── Format a Unix epoch (seconds) into a session-list time string ─
function formatSessionTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `${hh}:${mm}`;
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "昨天";
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

// ─── Parse sessions from /GetCurrentSession ──────────────────────
function parseSessions(
  raw: any,
  nameMap: Record<string, string>,
  lastMessages: Record<string, { content: string; type: string; is_sender: number; time: number; sender_wxid?: string }>,
): Session[] {
  if (!raw) return [];
  const list = raw.data || raw;
  if (!Array.isArray(list)) return [];

  return list
    .map((s: any, index: number) => {
      const wxid = s.strUsrName || s.wxid || "";
      if (shouldFilterSession(wxid)) return null;

      const lastMsg = lastMessages[wxid];
      let lastMsgPreview = "";
      let lastTimestamp = 0;

      if (lastMsg) {
        let content = lastMsg.content || "";
        let senderPrefix = "";
        // For group messages, resolve "senderWxid:\n" prefix to nickname
        if (wxid.includes("@chatroom")) {
          if (Number(lastMsg.is_sender) === 1) {
            senderPrefix = "我: ";
          } else {
            // Try parsing "senderWxid:\n" prefix from content (old WeChat format)
            const idx = content.indexOf(":\n");
            if (idx > 0 && idx < 60) {
              const senderWxid = content.substring(0, idx);
              senderPrefix = (nameMap[senderWxid] || senderWxid) + ": ";
              content = content.substring(idx + 2);
            } else if (lastMsg.sender_wxid) {
              // WeChat 4.x: sender wxid extracted from BytesExtra by backend
              const sw = lastMsg.sender_wxid;
              senderPrefix = (nameMap[sw] || sw) + ": ";
            }
          }
        }
        lastMsgPreview = senderPrefix + formatMsgTypePreview(lastMsg.type, content);
        lastTimestamp = lastMsg.time || 0;
      }

      return {
        wxid,
        nickname: nameMap[wxid] || s.strNickName || s.nickname || wxid,
        avatar: "",
        lastMsg: lastMsgPreview,
        lastTime: formatSessionTime(lastTimestamp),
        lastTimestamp,
        unread: 0,
        muted: false,
        order: Number.isFinite(Number(s.order)) ? Number(s.order) : index,
        is_group: wxid.includes("@chatroom"),
      } as Session;
    })
    .filter(Boolean) as Session[];
}

// ─── Build contact name map ──────────────────────────────────────
function buildContactMap(raw: any): Record<string, string> {
  const map: Record<string, string> = {};
  if (!raw) return map;
  const list = raw.friend || raw.data || (Array.isArray(raw) ? raw : []);
  for (const c of list) {
    const wxid = c.wxid || c.UserName || "";
    const name = c.markname || c.nickname || c.NickName || "";
    if (wxid && name) map[wxid] = name;
  }
  return map;
}

// ─── Build avatar URL map ────────────────────────────────────────
function buildAvatarMap(contacts: any, avatarUrls: Record<string, string> | undefined): Record<string, string> {
  const map: Record<string, string> = {};
  if (avatarUrls) {
    for (const [wxid, url] of Object.entries(avatarUrls)) {
      if (url) map[wxid] = url;
    }
  }
  if (contacts) {
    const list = contacts.friend || contacts.data || (Array.isArray(contacts) ? contacts : []);
    for (const c of list) {
      const wxid = c.wxid || c.UserName || "";
      if (!wxid || map[wxid]) continue;
      const url =
        c.headimgurl || c.head_img || c.head_big || c.head_small ||
        c.headimg || c.bigheadimgurl || c.smallheadimgurl || c.avatar || "";
      if (url) map[wxid] = url;
    }
  }
  return map;
}

function contactListFromRaw(raw: any): any[] {
  if (!raw) return [];
  const list = raw.friend || raw.data || (Array.isArray(raw) ? raw : []);
  return Array.isArray(list) ? list : [];
}

function chatroomListFromRaw(raw: any): any[] {
  if (!raw) return [];
  const list = raw.chatroom || raw.chatrooms || raw.group || raw.groups || [];
  return Array.isArray(list) ? list : [];
}

function profileDisplayName(profile: ContactProfile | undefined, fallback: string): string {
  const raw = profile?.profile || {};
  return (
    profile?.name ||
    raw.Remark ||
    raw.remark ||
    raw.markname ||
    raw.NickName ||
    raw.nickname ||
    fallback
  );
}

function profileAvatar(profile: ContactProfile | undefined, fallback = ""): string {
  const raw = profile?.profile || {};
  return (
    profile?.avatar ||
    raw.SmallHeadImgUrl ||
    raw.smallhead ||
    raw.BigHeadImgUrl ||
    raw.bighead ||
    fallback ||
    ""
  );
}

function profileArea(raw: Record<string, any> | undefined): string {
  if (!raw) return "";
  const country = String(raw.Country || raw.country || "").trim();
  const area = String(raw.Area || raw.city || "").trim();
  const province = String(raw.Province || raw.province || "").trim();
  const displayCountry = country && country !== "CN" ? country : "";
  return [displayCountry, area, province].filter(Boolean).join(" ");
}

// ─── Helpers ─────────────────────────────────────────────────────
function extractChatId(msg: any, selfWxid: string): string {
  if (msg.fromgid) return msg.fromgid;
  if (msg.fromid === selfWxid) return msg.toid;
  return msg.fromid;
}

function formatMsgTypePreview(msgType: string, content: string): string {
  const t = String(msgType);
  switch (t) {
    case "1": return replaceWechatEmojis(content?.substring(0, 50) || "");
    case "3": return "[图片]";
    case "34": return "[语音]";
    case "42": return "[名片]";
    case "43": return "[视频]";
    case "47": return "[表情]";
    case "48": return "[位置]";
    case "49": {
      if (content?.includes("<type>57</type>")) return extractQuoteTitle(content);
      if (content?.includes("<type>6</type>") || content?.includes("<type>74</type>")) return "[文件]";
      if (content?.includes("<type>5</type>")) return "[链接]";
      if (content?.includes("<type>33</type>") || content?.includes("<type>36</type>")) return "[小程序]";
      return "[链接/文件]";
    }
    case "10000": case "10002": return "[系统消息]";
    case "9994": return "";
    default: return content?.substring(0, 30) || "[消息]";
  }
}

function extractQuoteTitle(xml: string): string {
  try {
    const match = xml.match(/<title>(.*?)<\/title>/);
    return replaceWechatEmojis(match?.[1] || "[引用]");
  } catch {
    return "[引用]";
  }
}

/** Sort messages by timestamp ascending (oldest first). */
function sortByTimestamp(msgs: ChatMessage[]): ChatMessage[] {
  return msgs.slice().sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
}

function sortSessionsForDisplay(list: Session[]): Session[] {
  return list.slice().sort((a, b) => {
    const aOrder = Number.isFinite(a.order) ? Number(a.order) : Number.MAX_SAFE_INTEGER;
    const bOrder = Number.isFinite(b.order) ? Number(b.order) : Number.MAX_SAFE_INTEGER;
    if (aOrder !== bOrder) return aOrder - bOrder;
    const pinnedDelta = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
    if (pinnedDelta !== 0) return pinnedDelta;
    return (b.lastTimestamp || 0) - (a.lastTimestamp || 0);
  });
}

/**
 * Check whether a synthetic ``send_...`` placeholder matches a real message
 * that arrived later via callback or DB history.
 */
function isSyntheticMatch(synthetic: ChatMessage, real: ChatMessage): boolean {
  if (!String(synthetic.id).startsWith("send_")) return false;
  if (String(synthetic.msgtype) !== String(real.msgtype)) return false;
  const dt = Math.abs((synthetic.timestamp || 0) - (real.timestamp || 0));
  if (dt > 120) return false;
  // For text messages also require content match to avoid false positives
  if (String(real.msgtype) === "1" && String(synthetic.msg || "") !== String(real.msg || "")) return false;
  return true;
}

function isSelfSentMessage(msg: ChatMessage): boolean {
  return String(msg.sendorrecv) === "1" || msg.isSender === 1;
}

function isHookStatusEchoMessage(msg: ChatMessage): boolean {
  if (!isSelfSentMessage(msg)) return false;
  const msgType = String(msg.msgtype || "");
  const content = String(msg.msg || "").trim();
  if (msgType === "1" && !content) return true;
  if (msgType === "3" && ["PC发图片消息成功", "发图片消息成功"].includes(content)) return true;
  return false;
}

function isBareImageHashMessage(msg: ChatMessage): boolean {
  return (
    String(msg.msgtype || "") === "3"
    && /^[a-f0-9]{32}$/i.test(String(msg.msg || "").trim())
    && !msg.img_path
    && !msg.db_image_id
    && !msg.bytesExtraHex
  );
}

function closeInTime(a: ChatMessage, b: ChatMessage, seconds: number): boolean {
  const at = Number(a.timestamp || 0);
  const bt = Number(b.timestamp || 0);
  if (!at || !bt) return false;
  return Math.abs(at - bt) <= seconds;
}

/**
 * Given a list of messages, remove synthetic ``send_...`` placeholders that
 * have a corresponding real message (same type, similar timestamp, same content for text).
 */
function removeDuplicateSynthetics(msgs: ChatMessage[]): ChatMessage[] {
  const realSelfMsgs = msgs.filter(
    (m) => !String(m.id).startsWith("send_") && isSelfSentMessage(m),
  );
  if (realSelfMsgs.length === 0) return msgs;

  const syntheticIdsToRemove = new Set<string>();
  for (const real of realSelfMsgs) {
    const match = msgs.find(
      (m) => !syntheticIdsToRemove.has(m.id) && isSyntheticMatch(m, real),
    );
    if (match) syntheticIdsToRemove.add(match.id);
  }
  if (syntheticIdsToRemove.size === 0) return msgs;
  return msgs.filter((m) => !syntheticIdsToRemove.has(m.id));
}

function removeCallbackEchoes(msgs: ChatMessage[]): ChatMessage[] {
  return msgs.filter((msg) => {
    const id = String(msg.id || "");
    if (!id.startsWith("cb_") && !isBareImageHashMessage(msg)) return true;

    const msgType = String(msg.msgtype || "");
    const hasRealCompanion = msgs.some((other) => {
      if (other === msg) return false;
      if (String(other.id || "").startsWith("cb_")) return false;
      if (String(other.msgtype || "") !== msgType) return false;
      if (String(other.sendorrecv || "") !== String(msg.sendorrecv || "")) return false;
      if (!closeInTime(msg, other, 2)) return false;

      if (msgType === "1") {
        return String(other.msg || "") === String(msg.msg || "");
      }
      if (msgType === "3") {
        return Boolean(other.img_path || other.db_image_id || other.bytesExtraHex || String(other.msg || "").includes("<img"));
      }
      return false;
    });

    return !hasRealCompanion;
  });
}

function dedupeMessagesForDisplay(msgs: ChatMessage[]): ChatMessage[] {
  const visible = msgs.filter((msg) => !isHookStatusEchoMessage(msg));
  return removeDuplicateSynthetics(removeCallbackEchoes(visible));
}

function mergeMessagesById(existing: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const merged = [...existing];
  const seen = new Set(existing.map((m) => String(m.id)));
  for (const msg of incoming) {
    if (isHookStatusEchoMessage(msg)) continue;
    const id = String(msg.id || "");
    if (id && seen.has(id)) continue;
    if (id) seen.add(id);
    merged.push(msg);
  }
  return sortByTimestamp(dedupeMessagesForDisplay(merged));
}

function toChatMessage(msg: any, sendorrecv: string, myWxid: string): ChatMessage | null {
  if (!msg || typeof msg !== "object") return null;
  if (msg.msgtype && msg.fromid && msg.id) {
    return {
      ...msg,
      id: String(msg.id),
      msgtype: String(msg.msgtype || "1"),
      sendorrecv: String(msg.sendorrecv || sendorrecv || "2"),
      isSender: Number(msg.isSender ?? (String(msg.sendorrecv || sendorrecv) === "1" ? 1 : 0)),
      msg: String(msg.msg || ""),
      fromid: String(msg.fromid || ""),
      toid: String(msg.toid || ""),
      fromgid: String(msg.fromgid || ""),
      fromtype: String(msg.fromtype || ""),
      time: String(msg.time || ""),
      timestamp: Number(msg.timestamp || msg.time_unix || 0),
    } as ChatMessage;
  }

  const msgtype = String(msg.msgtype || "");
  if (msgtype === "9994") return null;
  let msgContent = msg.msg || "";
  if (msgtype === "1" && msgContent.includes("\n")) {
    msgContent = msgContent.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  }
  return {
    id: msg.msgsvrid || msg.clientmsgid || `msg_${Date.now()}_${Math.random()}`,
    msgtype,
    time: msg.time || "",
    timestamp: Number(msg.timestamp || msg.time_unix || 0),
    fromid: msg.fromid || myWxid,
    toid: msg.toid || "",
    fromgid: msg.fromgid || "",
    fromtype: msg.fromtype || "",
    msg: msgContent,
    sendorrecv,
    isSender: sendorrecv === "1" ? 1 : 0,
    img_path: msg.img_path,
    db_image_id: msg.db_image_id,
    img_len: msg.img_len,
    video_path: msg.video_path,
    voice_len: msg.voice_len,
    voice_hex: msg.voice_hex,
    voice_data: msg.voice_data,
    gif_path: msg.gif_path,
    file_path: msg.file_path,
    info: msg.info,
    msgsource: msg.msgsource,
  };
}

// ─── App Component ───────────────────────────────────────────────
export default function App() {
  const [authenticated, setAuthenticated] = useState(() => Boolean(getAccessKey()));
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [accounts, setAccounts] = useState<WeChatAccount[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [authError, setAuthError] = useState("");
  const [selfWxid, setSelfWxid] = useState("");
  const [sessions, setSessions] = useState<Session[]>([]);
  const [rawContacts, setRawContacts] = useState<any>(null);
  const [contactMap, setContactMap] = useState<Record<string, string>>({});
  const [avatarMap, setAvatarMap] = useState<Record<string, string>>({});
  const [contactProfiles, setContactProfiles] = useState<Record<string, ContactProfile>>({});
  const [activeChat, setActiveChat] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<Record<string, ChatMessage[]>>({});
  const [viewMode, setViewMode] = useState<ViewMode>("chats");
  const [contactsHydrating, setContactsHydrating] = useState(false);
  const [contactsHydrated, setContactsHydrated] = useState(false);
  const [selfCardOpen, setSelfCardOpen] = useState(false);
  const [selfProfileLoading, setSelfProfileLoading] = useState(false);
  const [selfImageOpen, setSelfImageOpen] = useState(false);

  // ─── Resolve avatars for group senders (incremental) ─────────────
  // Instead of fetching ALL group members (slow BatchGetContactBriefInfo),
  // we only resolve wxids that actually appear in loaded messages.
  const pendingBriefWxids = useRef<Set<string>>(new Set());
  const briefTimer = useRef<number | null>(null);
  const briefInFlight = useRef(false);
  const briefRequestedWxids = useRef<Set<string>>(new Set());
  const groupNamesFetched = useRef<Set<string>>(new Set());
  // Keep a live ref to avatarMap so flushBriefQueue always sees the latest
  const avatarMapRef = useRef(avatarMap);
  avatarMapRef.current = avatarMap;
  const contactMapRef = useRef(contactMap);
  contactMapRef.current = contactMap;
  const contactProfilesRef = useRef(contactProfiles);
  contactProfilesRef.current = contactProfiles;

  const resetChatState = useCallback(() => {
    setSelfWxid("");
    setSessions([]);
    setRawContacts(null);
    setContactMap({});
    setAvatarMap({});
    setContactProfiles({});
    setActiveChat(null);
    setChatMessages({});
    setViewMode("chats");
    setContactsHydrating(false);
    setContactsHydrated(false);
    pendingBriefWxids.current.clear();
    briefRequestedWxids.current.clear();
    groupNamesFetched.current.clear();
  }, []);

  const loadAccounts = useCallback(async () => {
    setAccountsLoading(true);
    try {
      const data = await getAccounts();
      if (data?.error === "unauthorized") {
        clearActiveAgentId();
        clearAccessKey();
        setAuthenticated(false);
        setSelectedAccountId("");
        setAccounts([]);
        resetChatState();
        return;
      }
      const rows = Array.isArray(data?.accounts) ? data.accounts : [];
      setAccounts(rows);
    } catch (err) {
      console.error("[ACCOUNTS] load failed:", err);
    } finally {
      setAccountsLoading(false);
    }
  }, [resetChatState]);

  const handleLogin = useCallback(async (key: string) => {
    setAuthError("");
    try {
      setAccessKey(key);
      const data = await loginWithKey(key);
      if (!data?.ok) {
        clearAccessKey();
        setAuthError("密钥不正确");
        return false;
      }
      setAuthenticated(true);
      await loadAccounts();
      return true;
    } catch {
      clearAccessKey();
      setAuthError("登录失败");
      return false;
    }
  }, [loadAccounts]);

  const handleSelectAccount = useCallback(async (account: WeChatAccount) => {
    const agentId = account.id;
    if (!agentId) return;
    resetChatState();
    const data = await activateAccount(agentId);
    if (!data?.ok) {
      await loadAccounts();
      return;
    }
    setActiveAgentId(agentId);
    setSelectedAccountId(agentId);
  }, [loadAccounts, resetChatState]);

  const handleLeaveAccount = useCallback(() => {
    clearActiveAgentId();
    resetChatState();
    setSelectedAccountId("");
    loadAccounts();
  }, [loadAccounts, resetChatState]);

  const handleLogout = useCallback(() => {
    clearActiveAgentId();
    clearAccessKey();
    setAuthenticated(false);
    setSelectedAccountId("");
    setAccounts([]);
    resetChatState();
  }, [resetChatState]);

  const applyContactProfileUpdates = useCallback((updates: Record<string, ContactProfile> | undefined) => {
    if (!updates || typeof updates !== "object" || Object.keys(updates).length === 0) return;

    const nextNames: Record<string, string> = {};
    const nextAvatars: Record<string, string> = {};

    for (const [wxid, entry] of Object.entries(updates)) {
      const name = entry?.name || entry?.profile?.Remark || entry?.profile?.NickName || "";
      const avatar = entry?.avatar ||
        entry?.profile?.SmallHeadImgUrl ||
        entry?.profile?.BigHeadImgUrl ||
        "";
      if (name && name !== wxid) nextNames[wxid] = name;
      if (avatar) nextAvatars[wxid] = avatar;
    }

    setContactProfiles((prev) => ({ ...prev, ...updates }));
    if (Object.keys(nextNames).length > 0) {
      setContactMap((prev) => ({ ...prev, ...nextNames }));
    }
    if (Object.keys(nextAvatars).length > 0) {
      setAvatarMap((prev) => ({ ...prev, ...nextAvatars }));
    }
    if (Object.keys(nextNames).length > 0 || Object.keys(nextAvatars).length > 0) {
      setSessions((prev) => prev.map((s) => ({
        ...s,
        nickname: nextNames[s.wxid] || s.nickname,
        avatar: nextAvatars[s.wxid] || s.avatar,
      })));
    }
  }, []);

  const ensureContactProfiles = useCallback(async (wxids: string[]) => {
    const unique = Array.from(new Set((wxids || []).filter(Boolean)));
    const cached: Record<string, ContactProfile> = {};
    const missing: string[] = [];
    for (const wxid of unique) {
      const hit = contactProfilesRef.current[wxid];
      if (hit?.profile && Object.keys(hit.profile).length > 0) {
        cached[wxid] = hit;
      } else {
        missing.push(wxid);
      }
    }
    if (missing.length === 0) return cached;

    const data = await getContactProfiles(missing);
    const members = data?.members || {};
    applyContactProfileUpdates(members);
    return { ...cached, ...members };
  }, [applyContactProfileUpdates]);

  const hydrateDirectoryContacts = useCallback(async () => {
    if (contactsHydrating || contactsHydrated) return;
    const friends = contactListFromRaw(rawContacts);
    const rooms = chatroomListFromRaw(rawContacts);
    const wxids = Array.from(new Set([
      ...friends.map((c: any) => c.wxid || c.UserName || ""),
      ...rooms.map((c: any) => c.wxid || c.UserName || c.strUsrName || ""),
    ].filter(Boolean)));
    if (wxids.length === 0) {
      setContactsHydrated(true);
      return;
    }
    setContactsHydrating(true);
    try {
      await ensureContactProfiles(wxids);
      setContactsHydrated(true);
    } catch (err) {
      console.error("[CONTACTS] hydrate failed:", err);
    } finally {
      setContactsHydrating(false);
    }
  }, [contactsHydrated, contactsHydrating, ensureContactProfiles, rawContacts]);

  const openSelfProfileCard = useCallback(async () => {
    if (!selfWxid) return;
    setSelfCardOpen(true);
    const hit = contactProfilesRef.current[selfWxid];
    if (hit?.profile && Object.keys(hit.profile).length > 0) return;
    setSelfProfileLoading(true);
    try {
      await ensureContactProfiles([selfWxid]);
    } catch (err) {
      console.error("[SELF_PROFILE]", err);
    } finally {
      setSelfProfileLoading(false);
    }
  }, [ensureContactProfiles, selfWxid]);

  const switchMode = useCallback((mode: ViewMode) => {
    setViewMode(mode);
    setActiveChat(null);
    if (mode === "contacts") {
      hydrateDirectoryContacts();
    }
  }, [hydrateDirectoryContacts]);

  const flushBriefQueue = useCallback(() => {
    if (briefInFlight.current) return;

    const wxids = Array.from(pendingBriefWxids.current);
    pendingBriefWxids.current.clear();
    if (wxids.length === 0) return;

    briefInFlight.current = true;
    batchGetContactBrief(wxids)
      .then((data: any) => {
        const members = data?.members;
        if (!members || typeof members !== "object") return;

        const newContacts: Record<string, string> = {};
        const newAvatars: Record<string, string> = {};

        for (const [wxid, info] of Object.entries<any>(members)) {
          const name = info?.name || "";
          const avatar = info?.avatar || "";
          if (name && name !== wxid) newContacts[wxid] = name;
          if (avatar) newAvatars[wxid] = avatar;
        }

        if (Object.keys(newContacts).length > 0) {
          setContactMap((prev) => {
            const merged = { ...prev };
            for (const [k, v] of Object.entries(newContacts)) {
              if (!merged[k]) merged[k] = v; // don't overwrite existing (friend remark / group names)
            }
            return merged;
          });
        }
        if (Object.keys(newAvatars).length > 0) {
          setAvatarMap((prev) => {
            const merged = { ...prev };
            for (const [k, v] of Object.entries(newAvatars)) {
              if (!merged[k]) merged[k] = v;
            }
            return merged;
          });
        }
        if (Object.keys(newContacts).length > 0 || Object.keys(newAvatars).length > 0) {
          setSessions((prev) => prev.map((s) => ({
            ...s,
            nickname: newContacts[s.wxid] || s.nickname,
            avatar: newAvatars[s.wxid] || s.avatar,
          })));
        }
      })
      .catch((err: Error) => console.error("[BRIEF] fetch failed:", err))
      .finally(() => {
        briefInFlight.current = false;
        // If more wxids arrived while we were in-flight, flush again soon.
        if (pendingBriefWxids.current.size > 0) {
          if (briefTimer.current == null) {
            briefTimer.current = window.setTimeout(() => {
              briefTimer.current = null;
              flushBriefQueue();
            }, 80);
          }
        }
      });
  }, []);

  const scheduleBriefFlush = useCallback(() => {
    if (briefTimer.current != null) return;
    briefTimer.current = window.setTimeout(() => {
      briefTimer.current = null;
      flushBriefQueue();
    }, 80);
  }, [flushBriefQueue]);

  /** Queue avatar resolution for a list of wxids (deduped, debounced). */
  const queueBriefLookup = useCallback((wxids: string[], mySelfWxid?: string) => {
    if (!wxids || wxids.length === 0) return;
    const selfId = mySelfWxid || selfWxid;
    const currentAvatars = avatarMapRef.current;
    const currentContacts = contactMapRef.current;
    for (const wxid of wxids) {
      if (!wxid) continue;
      if (wxid === selfId) continue;
      if (wxid.includes("@chatroom")) continue;
      const hasName = Boolean(currentContacts[wxid] && currentContacts[wxid] !== wxid);
      const hasAvatar = Boolean(currentAvatars[wxid]);
      if (hasName && hasAvatar) continue;
      // Already requested → skip
      if (briefRequestedWxids.current.has(wxid)) continue;
      briefRequestedWxids.current.add(wxid);
      pendingBriefWxids.current.add(wxid);
    }
    scheduleBriefFlush();
  }, [selfWxid, scheduleBriefFlush]);

  // ─── Fetch group member names (fast) on entering a group ──────────
  const fetchGroupMemberNames = useCallback((gid: string) => {
    if (groupNamesFetched.current.has(gid)) return;
    groupNamesFetched.current.add(gid);

    getGroupMemberNames(gid)
      .then((data: any) => {
        const names = data?.names;
        if (!names || typeof names !== "object") return;

        setContactMap((prev) => {
          const merged = { ...prev };
          for (const [wxid, name] of Object.entries<string>(names)) {
            if (!merged[wxid] && name) merged[wxid] = name;
          }
          return merged;
        });

        console.log(`[GROUP] Resolved ${Object.keys(names).length} member names for ${gid}`);
      })
      .catch((err: Error) => console.error("[GROUP] member names fetch failed:", err));
  }, []);

  // ─── WebSocket message handler ──────────────────────────────────
  const handleWSMessage = useCallback((wsMsg: WSMessage) => {
    const eventAccountId = String((wsMsg as any)?.data?.account_id || "");
    if (selectedAccountId && eventAccountId && eventAccountId !== selectedAccountId) return;
    if (wsMsg.type === "init") {
      const {
        self_info,
        contacts,
        sessions: rawSessions,
        last_messages,
        avatar_urls,
        messages_cache,
        session_cache,
      } = wsMsg.data as any;
      const wxid = self_info?.data?.wxid || self_info?.wxid || "";
      setSelfWxid(wxid);
      setRawContacts(contacts);

      const nameMap = buildContactMap(contacts);
      setContactMap(nameMap);

      const avatars = buildAvatarMap(contacts, avatar_urls);
      setAvatarMap(avatars);

      const parsed = parseSessions(rawSessions, nameMap, last_messages || {});
      let enriched: Session[] = parsed.map((s) => ({
        ...s,
        avatar: avatars[s.wxid] || "",
      }));
      const sessionCacheObj = (session_cache && typeof session_cache === "object") ? session_cache : {};
      const enrichedMap = new Map(enriched.map((s) => [s.wxid, s]));
      for (const [sessionWxid, snap] of Object.entries<any>(sessionCacheObj)) {
        const snapTs = Number(snap?.lastTimestamp || 0);
        const current = enrichedMap.get(sessionWxid);
        if (current) {
          const ts = snapTs || current.lastTimestamp || 0;
          enrichedMap.set(sessionWxid, {
            ...current,
            lastMsg: snap?.lastMsg || current.lastMsg,
            lastTime: formatSessionTime(ts) || current.lastTime,
            lastTimestamp: ts,
            unread: typeof snap?.unread === "number" ? snap.unread : current.unread,
          });
        } else {
          enrichedMap.set(sessionWxid, {
            wxid: sessionWxid,
            nickname: nameMap[sessionWxid] || sessionWxid,
            avatar: avatars[sessionWxid] || "",
            is_group: sessionWxid.includes("@chatroom"),
            lastMsg: snap?.lastMsg || "",
            lastTime: formatSessionTime(snapTs),
            lastTimestamp: snapTs,
            unread: typeof snap?.unread === "number" ? snap.unread : 0,
            muted: false,
          });
        }
      }
      enriched = sortSessionsForDisplay(Array.from(enrichedMap.values()));
      setSessions(enriched);

      const needsBrief = enriched
        .filter((s) => !s.is_group && (
          !avatars[s.wxid] ||
          (!nameMap[s.wxid] && (!s.nickname || s.nickname === s.wxid))
        ))
        .map((s) => s.wxid);
      queueBriefLookup(needsBrief, wxid);

      if (messages_cache && typeof messages_cache === "object") {
        setChatMessages((prev) => {
          const next = { ...prev };
          for (const [chatId, rows] of Object.entries<any>(messages_cache)) {
            if (!Array.isArray(rows)) continue;
            const normalized = rows
              .map((row) => toChatMessage(row, String(row?.sendorrecv || "2"), wxid))
              .filter(Boolean) as ChatMessage[];
            const existing = next[chatId] || [];
            next[chatId] = mergeMessagesById(existing, normalized);
          }
          return next;
        });
      }

      console.log("[INIT]", wxid,
        "sessions:", enriched.length,
        "contacts:", Object.keys(nameMap).length,
        "avatars:", Object.keys(avatars).length);
    }

    if (wsMsg.type === "wechat_message") {
      const { sendorrecv } = wsMsg.data as any;
      const msglist = ((wsMsg.data as any).messages || (wsMsg.data as any).msglist || []) as any[];
      const myWxid = wsMsg.data.selfwxid;
      if (!selfWxid && myWxid) setSelfWxid(myWxid);
      const contactUpdates = (wsMsg.data as any).contact_updates || {};
      applyContactProfileUpdates(contactUpdates);
      const liveContactMap = { ...contactMap };
      const liveAvatarMap = { ...avatarMap };
      for (const [wxid, entry] of Object.entries<any>(contactUpdates)) {
        const name = entry?.name || entry?.profile?.Remark || entry?.profile?.NickName || "";
        const avatar = entry?.avatar || entry?.profile?.SmallHeadImgUrl || entry?.profile?.BigHeadImgUrl || "";
        if (name && name !== wxid) liveContactMap[wxid] = name;
        if (avatar) liveAvatarMap[wxid] = avatar;
      }

      const groupSenderWxids = new Set<string>();
      const directChatWxids = new Set<string>();
      const chatsToAutoRead = new Set<string>();

      for (const msg of msglist) {
        const chatMsg = toChatMessage(msg, String(sendorrecv || "2"), myWxid || selfWxid);
        if (!chatMsg) continue;
        const chatId = extractChatId(chatMsg, myWxid || selfWxid);
        if (!chatId) continue;
        if (isHookStatusEchoMessage(chatMsg)) continue;

        const isIncoming = String(chatMsg.sendorrecv) === "2";
        const isCurrentlyViewing = activeChat === chatId;

        // If it's an incoming group message, queue brief lookup for the sender wxid.
        if (isIncoming && chatId.includes("@chatroom")) {
          const senderWxid = String(chatMsg.fromid || "");
          if (senderWxid && senderWxid !== myWxid) {
            groupSenderWxids.add(senderWxid);
          }
        }
        if (!chatId.includes("@chatroom")) {
          directChatWxids.add(chatId);
        }

        setChatMessages((prev) => {
          const existing = prev[chatId] || [];
          if (chatMsg.id && existing.some((m) => m.id === chatMsg.id)) return prev;

          // If this is a real self-sent message, replace the matching synthetic
          // "send_..." placeholder instead of appending a duplicate.
          const isSelfSent = String(chatMsg.sendorrecv) === "1" || chatMsg.isSender === 1;
          if (isSelfSent && String(chatMsg.msgtype) === "1" && !String(chatMsg.msg || "").trim()) {
            return prev;
          }
          if (isSelfSent && chatMsg.id && !String(chatMsg.id).startsWith("send_")) {
            const matchIdx = existing.findIndex((m) => isSyntheticMatch(m, chatMsg));
            if (matchIdx >= 0) {
              const updated = [...existing];
              updated[matchIdx] = chatMsg;
              return { ...prev, [chatId]: sortByTimestamp(dedupeMessagesForDisplay(updated)) };
            }
          }

          return { ...prev, [chatId]: sortByTimestamp(dedupeMessagesForDisplay([...existing, chatMsg])) };
        });

        // Update session list — move chat to top, update preview
        let preview = formatMsgTypePreview(chatMsg.msgtype, chatMsg.msg);
        // For group chats, prefix with sender nickname
        if (preview && chatId.includes("@chatroom")) {
          if (String(chatMsg.sendorrecv) === "1") {
            preview = `我: ${preview}`;
          } else {
            const senderWxid = String(chatMsg.fromid || "");
            if (senderWxid) {
              const senderName = liveContactMap[senderWxid] || senderWxid;
              preview = `${senderName}: ${preview}`;
            }
          }
        }
        if (preview) {
          const msgTs = chatMsg.timestamp || Math.floor(Date.now() / 1000);
          const timeStr = formatSessionTime(msgTs);
          setSessions((prev) => {
            const idx = prev.findIndex((s) => s.wxid === chatId);

            // Only increment unread if it's incoming AND user is NOT viewing this chat
            const unreadDelta = (isIncoming && !isCurrentlyViewing) ? 1 : 0;

            const session: Session = idx >= 0
              ? {
                  ...prev[idx],
                  lastMsg: preview,
                  lastTime: timeStr,
                  lastTimestamp: msgTs,
                  unread: isCurrentlyViewing ? 0 : (prev[idx].unread || 0) + unreadDelta,
                }
              : {
                  wxid: chatId,
                  nickname: liveContactMap[chatId] || chatMsg.fromid || chatId,
                  avatar: liveAvatarMap[chatId] || "",
                  is_group: chatId.includes("@chatroom"),
                  lastMsg: preview,
                  lastTime: timeStr,
                  lastTimestamp: msgTs,
                  unread: unreadDelta,
                  muted: false,
            };
            const rest = prev.filter((s) => s.wxid !== chatId);
            return sortSessionsForDisplay([session, ...rest]);
          });
        }

        // If the user is currently viewing this chat, mark it read on backend
        if (isCurrentlyViewing && isIncoming) {
          chatsToAutoRead.add(chatId);
        }
      }

      // Auto-mark-read for chats the user is currently viewing
      for (const cid of chatsToAutoRead) {
        markAsRead(cid).catch(() => {});
      }

      if (groupSenderWxids.size > 0) {
        queueBriefLookup(Array.from(groupSenderWxids));
      }
      if (directChatWxids.size > 0) {
        queueBriefLookup(Array.from(directChatWxids), myWxid || selfWxid);
      }
    }
    if (wsMsg.type === "message_sent") {
      const chatId = wsMsg.data.chat_id;
      const chatMsg = toChatMessage(wsMsg.data.message, "1", selfWxid);
      if (!chatId || !chatMsg) return;
      setChatMessages((prev) => {
        const existing = prev[chatId] || [];
        if (chatMsg.id && existing.some((m) => m.id === chatMsg.id)) return prev;
        if (String(chatMsg.id || "").startsWith("send_") && existing.some((m) => isSyntheticMatch(chatMsg, m))) {
          return prev;
        }
        return { ...prev, [chatId]: sortByTimestamp(dedupeMessagesForDisplay([...existing, chatMsg])) };
      });
      let preview = formatMsgTypePreview(chatMsg.msgtype, chatMsg.msg);
      // For group chats, prefix sent messages with "我:"
      if (preview && chatId.includes("@chatroom")) {
        preview = `我: ${preview}`;
      }
      const sentTs = chatMsg.timestamp || Math.floor(Date.now() / 1000);
      const timeStr = formatSessionTime(sentTs);
      setSessions((prev) => {
        const idx = prev.findIndex((s) => s.wxid === chatId);
        const session: Session = idx >= 0
          ? { ...prev[idx], lastMsg: preview, lastTime: timeStr, lastTimestamp: sentTs, unread: 0 }
          : {
              wxid: chatId,
              nickname: contactMap[chatId] || chatId,
              avatar: avatarMap[chatId] || "",
              is_group: chatId.includes("@chatroom"),
              lastMsg: preview,
              lastTime: timeStr,
              lastTimestamp: sentTs,
              unread: 0,
              muted: false,
            };
        const rest = prev.filter((s) => s.wxid !== chatId);
        return sortSessionsForDisplay([session, ...rest]);
      });
      // Also mark read on backend (sending implies viewing)
      markAsRead(chatId).catch(() => {});
    }

    // ─── mark_read: another frontend (or this one) read a chat ──────
    if (wsMsg.type === "mark_read") {
      const readWxid = (wsMsg.data as any)?.wxid;
      if (readWxid) {
        setSessions((prev) =>
          prev.map((s) => (s.wxid === readWxid ? { ...s, unread: 0 } : s))
        );
      }
    }
  }, [selfWxid, activeChat, contactMap, avatarMap, queueBriefLookup, applyContactProfileUpdates, selectedAccountId]);

  const { connected } = useWebSocket(handleWSMessage, authenticated && Boolean(selectedAccountId));

  useEffect(() => {
    if (!authenticated || selectedAccountId) return;
    loadAccounts();
    const timer = window.setInterval(loadAccounts, 3000);
    return () => window.clearInterval(timer);
  }, [authenticated, selectedAccountId, loadAccounts]);

  // ─── Periodic session refresh (picks up PC-sent messages missed by hook) ──
  useEffect(() => {
    // Only poll when on the session list (not inside a chat)
    if (activeChat) return;
    if (!connected) return;

    const refresh = () => {
      refreshSessions()
        .then((resp: any) => {
          if (!resp) return;
          const rawSessions = resp.sessions;
          const lastMessages = resp.last_messages || {};

          const parsed = parseSessions(rawSessions, contactMap, lastMessages);
          if (parsed.length === 0) return;

          setSessions((prev) => {
            // Merge: update existing sessions with fresh DB data, add any new ones
            const prevMap = new Map(prev.map((s) => [s.wxid, s]));
            for (const fresh of parsed) {
              const existing = prevMap.get(fresh.wxid);
              if (existing) {
                const hasNewerMessage = (fresh.lastTimestamp || 0) > (existing.lastTimestamp || 0);
                prevMap.set(fresh.wxid, {
                  ...existing,
                  nickname: existing.nickname || fresh.nickname,
                  avatar: existing.avatar || avatarMap[fresh.wxid] || fresh.avatar || "",
                  order: fresh.order,
                  lastMsg: hasNewerMessage ? (fresh.lastMsg || existing.lastMsg) : existing.lastMsg,
                  lastTime: hasNewerMessage ? (fresh.lastTime || existing.lastTime) : existing.lastTime,
                  lastTimestamp: hasNewerMessage ? fresh.lastTimestamp : existing.lastTimestamp,
                });
              } else {
                // New session not in our list yet
                prevMap.set(fresh.wxid, {
                  ...fresh,
                  avatar: avatarMap[fresh.wxid] || "",
                });
              }
            }
            return sortSessionsForDisplay(Array.from(prevMap.values()));
          });
        })
        .catch(() => {}); // silent fail
    };

    refresh();
    const timer = setInterval(refresh, 30_000);
    return () => clearInterval(timer);
  }, [activeChat, connected, contactMap, avatarMap]);

  // ─── Navigation (browser history integration for mobile back gesture) ──
  const handleSelectChat = (wxid: string, seed?: Partial<Session>) => {
    setViewMode("chats");
    setSessions((prev) => {
      if (prev.some((s) => s.wxid === wxid)) return prev;
      const seeded: Session = {
        wxid,
        nickname: seed?.nickname || contactMapRef.current[wxid] || wxid,
        avatar: seed?.avatar || avatarMapRef.current[wxid] || "",
        is_group: wxid.includes("@chatroom"),
        lastMsg: "",
        lastTime: "",
        lastTimestamp: 0,
        unread: 0,
        muted: false,
      };
      return sortSessionsForDisplay([seeded, ...prev]);
    });
    setActiveChat(wxid);
    // Push a history entry so mobile back gesture returns to session list
    window.history.pushState({ chat: wxid }, "");
    // Clear unread badge locally + notify backend (which broadcasts to all frontends)
    setSessions((prev) =>
      prev.map((s) => (s.wxid === wxid ? { ...s, unread: 0 } : s))
    );
    markAsRead(wxid).catch((err) => console.error("[MARK_READ]", err));
    // If it's a group, fetch member names (fast) + resolve avatars for loaded messages
    if (wxid.includes("@chatroom")) {
      fetchGroupMemberNames(wxid);
      // Also trigger brief-batch for senders already in loaded messages
      const existingMsgs = chatMessages[wxid] || [];
      if (existingMsgs.length > 0) {
        const senderWxids: string[] = [];
        for (const m of existingMsgs) {
          const from = m.fromid || "";
          if (from && !from.includes("@chatroom") && from !== selfWxid) {
            senderWxids.push(from);
          }
        }
        if (senderWxids.length > 0) {
          queueBriefLookup(senderWxids);
        }
      }
    }
  };

  const handleSessionMenuAction = async (action: SessionMenuAction, session: Session) => {
    const wxid = session.wxid;
    try {
      if (action === "pin") {
        await stickyChat(wxid);
        setSessions((prev) => sortSessionsForDisplay(prev.map((s) =>
          s.wxid === wxid ? { ...s, pinned: true } : s
        )));
        return;
      }
      if (action === "unpin") {
        await unpinChat(wxid);
        setSessions((prev) => sortSessionsForDisplay(prev.map((s) =>
          s.wxid === wxid ? { ...s, pinned: false } : s
        )));
        return;
      }
      if (action === "mark_unread") {
        await markSessionUnread(wxid);
        setSessions((prev) => prev.map((s) =>
          s.wxid === wxid ? { ...s, unread: Math.max(1, s.unread || 0) } : s
        ));
        return;
      }
      if (action === "mute") {
        await muteSession(wxid);
        setSessions((prev) => prev.map((s) =>
          s.wxid === wxid ? { ...s, muted: true } : s
        ));
        return;
      }
      if (action === "unmute") {
        await unmuteSession(wxid);
        setSessions((prev) => prev.map((s) =>
          s.wxid === wxid ? { ...s, muted: false } : s
        ));
        return;
      }
      if (action === "delete") {
        setSessions((prev) => prev.filter((s) => s.wxid !== wxid));
        setChatMessages((prev) => {
          const next = { ...prev };
          delete next[wxid];
          return next;
        });
        if (activeChat === wxid) {
          setActiveChat(null);
        }
      }
    } catch (err) {
      console.error("[SESSION_ACTION]", action, wxid, err);
    }
  };

  const handleBack = () => {
    setActiveChat(null);
    hasUnsavedInput.current = false;
    // If we pushed a state for this chat, go back to remove it
    if (window.history.state?.chat) {
      window.history.back();
    }
  };

  // Listen for browser back button / swipe-back gesture
  useEffect(() => {
    const onPopState = (_e: PopStateEvent) => {
      // If we're in a chat and user pressed back, return to session list
      setActiveChat((current) => {
        if (current) return null;   // was in chat → go to session list
        return current;
      });
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // ─── Prevent accidental page close only when there's unsent text ───
  const hasUnsavedInput = useRef(false);
  const setHasUnsavedInput = useCallback((val: boolean) => {
    hasUnsavedInput.current = val;
  }, []);

  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      // Only prompt when the user has typed something unsent
      if (!hasUnsavedInput.current) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const handleNewMessages = (wxid: string, msgs: ChatMessage[]) => {
    const displayMsgs = msgs.filter((msg) => !isHookStatusEchoMessage(msg));
    setChatMessages((prev) => {
      const existing = prev[wxid] || [];
      // DB history is authoritative — replace any existing messages with the same ID
      // (callback versions may have wrong sender/timestamp)
      const incomingById = new Map(displayMsgs.map((m) => [m.id, m]));
      let merged = existing.map((m) => incomingById.get(m.id) || m);
      // Add any incoming messages that weren't already present
      const existingIds = new Set(existing.map((m) => m.id));
      for (const m of displayMsgs) {
        if (!existingIds.has(m.id)) merged.push(m);
      }
      // Remove synthetic send_... placeholders that now have a real counterpart
      merged = dedupeMessagesForDisplay(merged);
      return { ...prev, [wxid]: sortByTimestamp(merged) };
    });

    // Update session list preview based on the latest message from loaded history.
    // This ensures that even if a real-time callback was missed (e.g. mobile-sent
    // messages), the session list updates when the chat is opened.
    if (displayMsgs.length > 0) {
      const latest = displayMsgs.reduce((a, b) => ((a.timestamp || 0) >= (b.timestamp || 0) ? a : b));
      let preview = formatMsgTypePreview(latest.msgtype, latest.msg) || "[消息]";
      if (wxid.includes("@chatroom")) {
        if (String(latest.sendorrecv) === "1" || latest.isSender === 1) {
          preview = `我: ${preview}`;
        } else {
          const senderWxid = String(latest.fromid || "");
          if (senderWxid) {
            const senderName = contactMap[senderWxid] || senderWxid;
            preview = `${senderName}: ${preview}`;
          }
        }
      }
      const msgTs = latest.timestamp || Math.floor(Date.now() / 1000);
      const timeStr = formatSessionTime(msgTs);
      setSessions((prev) => {
        const idx = prev.findIndex((s) => s.wxid === wxid);
        if (idx < 0) return prev; // Don't create new sessions from history
        const existing = prev[idx];
        // Only update if this message is newer than what the session already shows
        if (msgTs <= (existing.lastTimestamp || 0)) return prev;
        const updated = { ...existing, lastMsg: preview, lastTime: timeStr, lastTimestamp: msgTs };
        const rest = prev.filter((s) => s.wxid !== wxid);
        return sortSessionsForDisplay([updated, ...rest]);
      });
    }

    // For group chats, resolve brief info only for senders that appear in loaded messages.
    if (wxid.includes("@chatroom")) {
      const senderWxids = new Set<string>();
      for (const m of displayMsgs) {
        const from = m.fromid || "";
        if (!from) continue;
        if (from === selfWxid) continue;
        if (from.includes("@chatroom")) continue;
        senderWxids.add(from);
      }
      if (senderWxids.size > 0) {
        queueBriefLookup(Array.from(senderWxids));
      }
    }
  };

  const friendEntries: DirectoryEntry[] = contactListFromRaw(rawContacts)
    .map((c: any) => {
      const wxid = c.wxid || c.UserName || "";
      if (!wxid || shouldFilterSession(wxid)) return null;
      const profile = contactProfiles[wxid];
      const fallbackName = c.markname || c.nickname || c.NickName || contactMap[wxid] || wxid;
      const fallbackAvatar =
        avatarMap[wxid] ||
        c.smallhead ||
        c.bighead ||
        c.headimgurl ||
        c.head_img ||
        c.head_big ||
        c.head_small ||
        "";
      return {
        wxid,
        name: profileDisplayName(profile, fallbackName),
        avatar: profileAvatar(profile, fallbackAvatar),
        is_group: false,
        source: "friend" as const,
      };
    })
    .filter(Boolean) as DirectoryEntry[];

  const rawRoomEntries = chatroomListFromRaw(rawContacts)
    .map((c: any) => ({
      wxid: c.wxid || c.UserName || c.strUsrName || "",
      name: c.nickname || c.NickName || c.strNickName || "",
      avatar: c.smallhead || c.bighead || "",
    }))
    .filter((c: any) => c.wxid);
  const sessionRoomEntries = sessions
    .filter((s) => s.is_group)
    .map((s) => ({ wxid: s.wxid, name: s.nickname, avatar: s.avatar || avatarMap[s.wxid] || "" }));
  const groupEntryMap = new Map<string, DirectoryEntry>();
  for (const room of [...rawRoomEntries, ...sessionRoomEntries]) {
    if (!room.wxid) continue;
    const profile = contactProfiles[room.wxid];
    groupEntryMap.set(room.wxid, {
      wxid: room.wxid,
      name: profileDisplayName(profile, room.name || contactMap[room.wxid] || room.wxid),
      avatar: profileAvatar(profile, room.avatar || avatarMap[room.wxid] || ""),
      is_group: true,
      source: "group",
    });
  }
  const groupEntries = Array.from(groupEntryMap.values());
  const selfProfile = selfWxid ? contactProfiles[selfWxid] : undefined;
  const selfInfoName = contactMap[selfWxid] || (selfProfile ? profileDisplayName(selfProfile, "") : "") || "我";
  const selfAvatar =
    profileAvatar(selfProfile, avatarMap[selfWxid] || "") ||
    (selfProfile?.profile?.BigHeadImgUrl || selfProfile?.profile?.SmallHeadImgUrl || "");
  const activeSession = sessions.find((s) => s.wxid === activeChat) || (activeChat ? {
    wxid: activeChat,
    nickname: contactMap[activeChat] || activeChat,
    avatar: avatarMap[activeChat] || "",
    is_group: activeChat.includes("@chatroom"),
    lastMsg: "",
    lastTime: "",
    lastTimestamp: 0,
    unread: 0,
    muted: false,
  } as Session : null);
  const activeMsgs = activeChat ? (chatMessages[activeChat] || []) : [];

  if (!authenticated) {
    return <AccessGate onLogin={handleLogin} error={authError} />;
  }

  if (!selectedAccountId) {
    return (
      <AccountPortal
        accounts={accounts}
        loading={accountsLoading}
        onRefresh={loadAccounts}
        onSelectAccount={handleSelectAccount}
        onLogout={handleLogout}
      />
    );
  }

  return (
    <div className="h-dvh w-screen bg-[#f5f5f5] overflow-hidden relative flex">
      {/* Connection status */}
      {!connected && (
        <div className="fixed top-0 left-0 right-0 bg-[#e6a23c] text-black text-center text-[12px] py-1 z-50">
          正在连接后端服务器...
        </div>
      )}

      <WorkspaceSidebar
        mode={viewMode}
        selfName={selfInfoName}
        selfAvatar={selfAvatar}
        onSelfClick={openSelfProfileCard}
        onModeChange={switchMode}
        onBackToAccounts={handleLeaveAccount}
      />

      <div className="w-[272px] shrink-0 border-r border-[#d8d8d8] bg-[#e9e8e8] h-full">
        {viewMode === "chats" && (
          <SessionList
            sessions={sessions}
            activeWxid={activeChat}
            onSelectChat={handleSelectChat}
            onSessionAction={handleSessionMenuAction}
          />
        )}
        {viewMode === "contacts" && (
          <ContactsPanel
            friends={friendEntries}
            groups={groupEntries}
            loading={contactsHydrating}
            onHydrate={hydrateDirectoryContacts}
            onSelect={(entry) => handleSelectChat(entry.wxid, {
              nickname: entry.name,
              avatar: entry.avatar,
              is_group: entry.is_group,
            })}
          />
        )}
        {viewMode === "broadcast" && (
          <BroadcastPanel
            friends={friendEntries}
            groups={groupEntries}
          />
        )}
      </div>

      <div className="flex-1 min-w-0 h-full bg-[#111111]">
        {activeChat && activeSession ? (
          <ChatArea
            session={activeSession}
            messages={activeMsgs}
            selfWxid={selfWxid}
            onBack={handleBack}
            onNewMessages={handleNewMessages}
            avatarMap={avatarMap}
            contactMap={contactMap}
            contactProfiles={contactProfiles}
            onRequestContactProfile={ensureContactProfiles}
            onInputChange={setHasUnsavedInput}
          />
        ) : (
          <EmptyChatPane />
        )}
      </div>

      {selfCardOpen && (
        <SelfProfileCard
          profile={selfProfile}
          fallbackName={selfInfoName}
          fallbackAvatar={selfAvatar}
          loading={selfProfileLoading}
          onClose={() => setSelfCardOpen(false)}
          onAvatarClick={() => setSelfImageOpen(true)}
        />
      )}

      {selfImageOpen && (
        <LargeAvatarOverlay
          src={
            selfProfile?.profile?.BigHeadImgUrl ||
            selfProfile?.profile?.head_big ||
            selfAvatar
          }
          onClose={() => setSelfImageOpen(false)}
        />
      )}
    </div>
  );
}

function AccessGate({
  onLogin,
  error,
}: {
  onLogin: (key: string) => Promise<boolean>;
  error: string;
}) {
  const [key, setKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!key.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onLogin(key.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="h-dvh w-screen bg-[#111111] text-[#e8e8e8] flex items-center justify-center">
      <form onSubmit={submit} className="w-[360px] max-w-[calc(100vw-40px)]">
        <div className="text-[24px] font-medium mb-[22px]">访问密钥</div>
        <input
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoFocus
          type="password"
          className="w-full h-[44px] rounded-[4px] bg-[#222] border border-[#3a3a3a] px-[12px] outline-none focus:border-[#07c160]"
          placeholder="请输入 key"
        />
        {error && <div className="mt-[10px] text-[13px] text-[#f56c6c]">{error}</div>}
        <button
          type="submit"
          disabled={submitting || !key.trim()}
          className="mt-[16px] w-full h-[42px] rounded-[4px] bg-[#07c160] text-white disabled:bg-[#315541] active:opacity-85"
        >
          {submitting ? "验证中" : "进入"}
        </button>
      </form>
    </div>
  );
}

function AccountPortal({
  accounts,
  loading,
  onRefresh,
  onSelectAccount,
  onLogout,
}: {
  accounts: WeChatAccount[];
  loading: boolean;
  onRefresh: () => void;
  onSelectAccount: (account: WeChatAccount) => void;
  onLogout: () => void;
}) {
  return (
    <div className="h-dvh w-screen bg-[#111111] text-[#e8e8e8] overflow-hidden flex">
      <div className="w-[420px] max-w-[44vw] min-w-[340px] border-r border-[#2b2b2b] h-full flex flex-col">
        <div className="h-[78px] px-[24px] flex items-center justify-between border-b border-[#242424]">
          <div>
            <div className="text-[22px] font-medium">微信账号</div>
            <div className="text-[12px] text-[#777] mt-[3px]">在线 {accounts.length} 个</div>
          </div>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onRefresh}
              className="h-[32px] px-[10px] rounded-[4px] bg-[#242424] text-[#cfcfcf] active:bg-[#303030]"
            >
              刷新
            </button>
            <button
              type="button"
              onClick={onLogout}
              className="h-[32px] px-[10px] rounded-[4px] bg-[#242424] text-[#cfcfcf] active:bg-[#303030]"
            >
              退出
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-[18px]">
          {loading && accounts.length === 0 && <div className="text-[#777] text-[14px]">正在读取在线微信...</div>}
          {accounts.length === 0 && !loading && (
            <div className="text-[#777] text-[14px] leading-[24px]">
              暂无在线微信。请让客户端 DLL 连接到当前后端 `/agent`。
            </div>
          )}
          <div className="space-y-[12px]">
            {accounts.map((account) => (
              <button
                key={account.id}
                type="button"
                onClick={() => onSelectAccount(account)}
                className="w-full min-h-[82px] rounded-[6px] bg-[#1b1b1b] hover:bg-[#242424] active:bg-[#2b2b2b] border border-[#2b2b2b] p-[14px] flex items-center gap-[14px] text-left"
              >
                <AccountAvatar account={account} />
                <div className="min-w-0 flex-1">
                  <div className="text-[18px] truncate">{account.nickname || account.wxid || account.id}</div>
                  <div className="text-[12px] text-[#888] truncate mt-[5px]">{account.wxid || account.account_id || account.id}</div>
                  <div className="text-[12px] text-[#666] truncate mt-[3px]">{account.peer || "connected"}</div>
                </div>
                <div className={`text-[12px] px-[7px] py-[3px] rounded-[4px] ${
                  account.initialized ? "bg-[#123d27] text-[#49d17d]" : "bg-[#3d3112] text-[#e6bd51]"
                }`}>
                  {account.initialized ? "已就绪" : "初始化中"}
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="flex-1 min-w-0 h-full">
        <MultiAccountBroadcastPanel accounts={accounts} />
      </div>
    </div>
  );
}

function AccountAvatar({ account }: { account: WeChatAccount }) {
  const [failed, setFailed] = useState(false);
  const name = account.nickname || account.wxid || account.id || "?";
  if (account.avatar && !failed) {
    return <img src={account.avatar} alt="" className="w-[54px] h-[54px] rounded-[5px] object-cover bg-[#333]" onError={() => setFailed(true)} />;
  }
  return (
    <div className="w-[54px] h-[54px] rounded-[5px] bg-[#07c160] text-white flex items-center justify-center text-[22px] shrink-0">
      {name[0]}
    </div>
  );
}

function MultiAccountBroadcastPanel({ accounts }: { accounts: WeChatAccount[] }) {
  const [targetText, setTargetText] = useState("");
  const [message, setMessage] = useState("");
  const [image, setImage] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(new Set());
  const [sending, setSending] = useState(false);
  const [resultText, setResultText] = useState("");

  useEffect(() => {
    setSelectedAgents(new Set(accounts.map((a) => a.id).filter(Boolean)));
  }, [accounts]);

  useEffect(() => {
    if (!image) {
      setPreview("");
      return;
    }
    const url = URL.createObjectURL(image);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [image]);

  const targets = targetText
    .split(/[\s,，;；]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  const agentIds = Array.from(selectedAgents).filter(Boolean);

  const toggleAgent = (id: string) => {
    setSelectedAgents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const sendText = async () => {
    if (!message.trim() || targets.length === 0 || agentIds.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastText(agentIds, targets, message.trim());
      setResultText(`文本完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  const sendImage = async () => {
    if (!image || targets.length === 0 || agentIds.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastImageUpload(agentIds, targets, image);
      setResultText(`图片完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto p-[28px]">
      <div className="max-w-[760px]">
        <div className="text-[22px] font-medium">多号群发</div>
        <div className="mt-[18px] grid grid-cols-1 gap-[14px]">
          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">发送账号</div>
            <div className="flex flex-wrap gap-[8px]">
              {accounts.map((account) => (
                <label key={account.id} className="h-[34px] px-[10px] rounded-[4px] bg-[#1d1d1d] border border-[#303030] flex items-center gap-[7px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={selectedAgents.has(account.id)}
                    onChange={() => toggleAgent(account.id)}
                    className="accent-[#07c160]"
                  />
                  <span className="text-[13px]">{account.nickname || account.wxid || account.id}</span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">目标 wxid / 群 id</div>
            <textarea
              value={targetText}
              onChange={(e) => setTargetText(e.target.value)}
              className="w-full h-[92px] resize-none rounded-[4px] bg-[#1d1d1d] border border-[#303030] outline-none px-[10px] py-[8px] text-[14px] focus:border-[#07c160]"
              placeholder="每行一个，或用逗号分隔"
            />
          </div>

          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">文本消息</div>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              className="w-full h-[100px] resize-none rounded-[4px] bg-[#1d1d1d] border border-[#303030] outline-none px-[10px] py-[8px] text-[14px] focus:border-[#07c160]"
              placeholder="输入文本"
            />
            <button
              type="button"
              disabled={sending || !message.trim() || targets.length === 0 || agentIds.length === 0}
              onClick={sendText}
              className="mt-[10px] h-[36px] px-[18px] rounded-[4px] bg-[#07c160] text-white disabled:bg-[#315541] active:opacity-85"
            >
              {sending ? "发送中" : "群发文本"}
            </button>
          </div>

          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">图片消息</div>
            <input
              type="file"
              accept="image/*"
              onChange={(e) => setImage(e.target.files?.[0] || null)}
              className="block text-[13px] text-[#aaa]"
            />
            {preview && <img src={preview} alt="" className="mt-[10px] max-w-[180px] max-h-[140px] rounded-[4px] object-contain bg-[#1d1d1d]" />}
            <button
              type="button"
              disabled={sending || !image || targets.length === 0 || agentIds.length === 0}
              onClick={sendImage}
              className="mt-[10px] h-[36px] px-[18px] rounded-[4px] bg-[#07c160] text-white disabled:bg-[#315541] active:opacity-85"
            >
              {sending ? "发送中" : "群发图片"}
            </button>
          </div>

          <div className="text-[13px] text-[#888]">
            已选账号 {agentIds.length} 个，目标 {targets.length} 个。{resultText}
          </div>
        </div>
      </div>
    </div>
  );
}

function WorkspaceSidebar({
  mode,
  selfName,
  selfAvatar,
  onSelfClick,
  onModeChange,
  onBackToAccounts,
}: {
  mode: ViewMode;
  selfName: string;
  selfAvatar: string;
  onSelfClick: () => void;
  onModeChange: (mode: ViewMode) => void;
  onBackToAccounts: () => void;
}) {
  return (
    <div className="w-[56px] shrink-0 h-full bg-[#2e2e2e] flex flex-col items-center py-[14px]">
      <button
        type="button"
        className="w-[42px] h-[42px] rounded-[2px] overflow-hidden bg-[#111] active:opacity-80"
        onClick={onSelfClick}
        title={selfName}
      >
        {selfAvatar ? (
          <img src={selfAvatar} alt="" className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full bg-[#576b95] text-white flex items-center justify-center text-[22px]">
            {(selfName || "我")[0]}
          </div>
        )}
      </button>

      <div className="mt-[30px] flex flex-col items-center gap-[24px]">
        <SidebarIconButton
          active={mode === "chats"}
          title="聊天"
          onClick={() => onModeChange("chats")}
          icon={<path d="M4 6.5A3.5 3.5 0 0 1 7.5 3h9A3.5 3.5 0 0 1 20 6.5v5A3.5 3.5 0 0 1 16.5 15H11l-5 4v-4.35A3.5 3.5 0 0 1 4 11.5v-5Z" />}
        />
        <SidebarIconButton
          active={mode === "contacts"}
          title="联系人"
          onClick={() => onModeChange("contacts")}
          icon={<path d="M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0ZM4.5 21c.8-4.2 3.3-6.3 7.5-6.3s6.7 2.1 7.5 6.3M18 5v4M20 7h-4" />}
        />
        <SidebarIconButton
          active={mode === "broadcast"}
          title="群发"
          onClick={() => onModeChange("broadcast")}
          icon={<path d="M5 7.5h8.5a3.5 3.5 0 0 1 0 7H8l-4 3v-6A4 4 0 0 1 5 7.5Zm11.5 2.2 3.5-2.2v7l-3.5-2.2V9.7Z" />}
        />
      </div>

      <button
        type="button"
        title="返回账号"
        onClick={onBackToAccounts}
        className="mt-auto mb-[12px] w-[40px] h-[40px] flex items-center justify-center text-[#9b9b9b] hover:text-[#07c160] active:opacity-75"
      >
        <svg className="w-[25px] h-[25px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M15 18 9 12l6-6" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M10 12h10" />
        </svg>
      </button>
    </div>
  );
}

function SidebarIconButton({
  active,
  title,
  icon,
  onClick,
}: {
  active: boolean;
  title: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`w-[40px] h-[40px] flex items-center justify-center active:opacity-75 ${
        active ? "text-[#07c160]" : "text-[#9b9b9b]"
      }`}
    >
      <svg className="w-[27px] h-[27px]" fill="none" stroke="currentColor" strokeWidth={1.75} viewBox="0 0 24 24">
        {icon}
      </svg>
    </button>
  );
}

function EntryAvatar({ entry }: { entry: DirectoryEntry }) {
  const [failed, setFailed] = useState(false);
  if (entry.avatar && !failed) {
    return (
      <img
        src={entry.avatar}
        alt=""
        className="w-[42px] h-[42px] rounded-[4px] object-cover bg-[#ddd] shrink-0"
        onError={() => setFailed(true)}
        loading="lazy"
      />
    );
  }
  return (
    <div className={`w-[42px] h-[42px] rounded-[4px] flex items-center justify-center text-white text-[16px] shrink-0 ${
      entry.is_group ? "bg-[#4f8dd8]" : "bg-[#07c160]"
    }`}>
      {(entry.name || entry.wxid || "?")[0]}
    </div>
  );
}

function ContactsPanel({
  friends,
  groups,
  loading,
  onHydrate,
  onSelect,
}: {
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
  loading: boolean;
  onHydrate: () => void;
  onSelect: (entry: DirectoryEntry) => void;
}) {
  const [query, setQuery] = useState("");
  useEffect(() => {
    onHydrate();
  }, [onHydrate]);

  const q = query.trim().toLowerCase();
  const filterEntry = (entry: DirectoryEntry) =>
    !q || entry.name.toLowerCase().includes(q) || entry.wxid.toLowerCase().includes(q);
  const visibleFriends = friends.filter(filterEntry);
  const visibleGroups = groups.filter(filterEntry);

  return (
    <div className="h-full flex flex-col bg-[#e9e8e8] text-[#111]">
      <div className="h-[92px] px-[18px] flex items-center gap-[12px] shrink-0">
        <div className="flex-1 h-[38px] bg-[#dcdcdc] rounded-[4px] flex items-center px-[10px]">
          <svg className="w-[18px] h-[18px] text-[#777] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="ml-[8px] bg-transparent outline-none text-[15px] w-full placeholder-[#888]"
            placeholder="搜索"
          />
        </div>
        <button
          type="button"
          className="w-[38px] h-[38px] rounded-[4px] bg-[#dcdcdc] text-[#555] flex items-center justify-center active:bg-[#d0d0d0]"
          title="刷新详情"
          onClick={onHydrate}
        >
          <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path d="M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0ZM4.5 21c.8-4.2 3.3-6.3 7.5-6.3s6.7 2.1 7.5 6.3M18 5v4M20 7h-4" />
          </svg>
        </button>
      </div>

      {loading && (
        <div className="px-[18px] pb-[8px] text-[12px] text-[#888] shrink-0">
          正在通过 GetContact 批量补全联系人资料...
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <ContactSection title="好友" entries={visibleFriends} onSelect={onSelect} />
        <ContactSection title="群聊" entries={visibleGroups} onSelect={onSelect} />
      </div>
    </div>
  );
}

function ContactSection({
  title,
  entries,
  onSelect,
}: {
  title: string;
  entries: DirectoryEntry[];
  onSelect: (entry: DirectoryEntry) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <div>
      <div className="px-[18px] py-[10px] text-[14px] text-[#8a8a8a]">{title}</div>
      {entries.map((entry) => (
        <button
          key={`${entry.source}_${entry.wxid}`}
          type="button"
          onClick={() => onSelect(entry)}
        className="w-full h-[64px] px-[14px] flex items-center gap-[12px] text-left hover:bg-[#dedede] active:bg-[#d3d3d3]"
        >
          <EntryAvatar entry={entry} />
          <div className="min-w-0 flex-1">
            <div className="text-[16px] text-[#111] truncate">{entry.name || entry.wxid}</div>
            {entry.wxid && entry.wxid !== entry.name && (
              <div className="text-[12px] text-[#999] truncate mt-[3px]">{entry.wxid}</div>
            )}
          </div>
        </button>
      ))}
    </div>
  );
}

function BroadcastPanel({
  friends,
  groups,
}: {
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const [broadcastImages, setBroadcastImages] = useState<BroadcastImageItem[]>([]);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(0);
  const [failed, setFailed] = useState(0);
  const messageInputRef = useRef<HTMLTextAreaElement>(null);
  const imageOrdinalRef = useRef(1);
  const previewUrlsRef = useRef<string[]>([]);

  const targets = [...friends, ...groups];
  const targetMap = new Map(targets.map((entry) => [entry.wxid, entry]));
  const q = query.trim().toLowerCase();
  const visible = targets.filter((entry) =>
    !q || entry.name.toLowerCase().includes(q) || entry.wxid.toLowerCase().includes(q)
  );
  const payloadParts = buildBroadcastParts(message, broadcastImages);
  const hasPayload = payloadParts.length > 0;

  useEffect(() => {
    return () => {
      for (const url of previewUrlsRef.current) URL.revokeObjectURL(url);
      previewUrlsRef.current = [];
    };
  }, []);

  const toggle = (wxid: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(wxid)) next.delete(wxid);
      else next.add(wxid);
      return next;
    });
  };

  const selectEntries = (entries: DirectoryEntry[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const entry of entries) next.add(entry.wxid);
      return next;
    });
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const clipboardFiles = Array.from(e.clipboardData?.files || []);
    const fileFromFiles = clipboardFiles.find((file) => file.type.startsWith("image/"));
    const items = Array.from(e.clipboardData?.items || []);
    const fileFromItems = items
      .find((item) => item.kind === "file" && item.type.startsWith("image/"))
      ?.getAsFile();
    const image = fileFromFiles || fileFromItems;
    if (!image) return;

    e.preventDefault();
    e.stopPropagation();
    const ordinal = imageOrdinalRef.current++;
    const token = `【图片${ordinal}】`;
    const preview = URL.createObjectURL(image);
    previewUrlsRef.current.push(preview);
    setBroadcastImages((prev) => [
      ...prev,
      {
        id: `broadcast_img_${Date.now()}_${ordinal}`,
        token,
        label: `图片${ordinal}`,
        file: image,
        preview,
      },
    ]);

    const input = messageInputRef.current;
    const start = input?.selectionStart ?? message.length;
    const end = input?.selectionEnd ?? message.length;
    const next = message.slice(0, start) + token + message.slice(end);
    setMessage(next);
    requestAnimationFrame(() => {
      if (!input) return;
      input.focus();
      const caret = start + token.length;
      input.selectionStart = caret;
      input.selectionEnd = caret;
    });
  };

  const removeBroadcastImage = (image: BroadcastImageItem) => {
    URL.revokeObjectURL(image.preview);
    previewUrlsRef.current = previewUrlsRef.current.filter((url) => url !== image.preview);
    setBroadcastImages((prev) => prev.filter((item) => item.id !== image.id));
    setMessage((prev) => prev.split(image.token).join(""));
  };

  const sendBroadcast = async () => {
    const parts = buildBroadcastParts(message, broadcastImages);
    const wxids = Array.from(selected).filter((wxid) => targetMap.has(wxid));
    if (parts.length === 0 || wxids.length === 0 || sending) return;
    setSending(true);
    setSent(0);
    setFailed(0);
    const status = new Map(wxids.map((wxid) => [wxid, true]));
    const updateProgress = () => {
      let ok = 0;
      let bad = 0;
      for (const value of status.values()) {
        if (value) ok += 1;
        else bad += 1;
      }
      setSent(ok);
      setFailed(bad);
    };
    const applyResult = (res: any, targets: string[]) => {
      const rows = Array.isArray(res?.results) ? res.results : [];
      if (rows.length === 0 && res?.error) {
        for (const wxid of targets) status.set(wxid, false);
        return;
      }
      const seen = new Set<string>();
      for (const row of rows) {
        const wxid = String(row?.wxid || "");
        if (!wxid) continue;
        seen.add(wxid);
        if (!row?.ok) status.set(wxid, false);
      }
      for (const wxid of targets) {
        if (!seen.has(wxid)) status.set(wxid, false);
      }
    };
    try {
      for (const part of parts) {
        const activeWxids = wxids.filter((wxid) => status.get(wxid));
        if (activeWxids.length === 0) break;
        try {
          const res = part.type === "text"
            ? await broadcastText(activeWxids, part.text)
            : await broadcastImageUpload(activeWxids, part.image.file);
          applyResult(res, activeWxids);
        } catch {
          for (const wxid of activeWxids) status.set(wxid, false);
        }
        updateProgress();
        await new Promise((resolve) => window.setTimeout(resolve, 120));
      }
    } finally {
      updateProgress();
      setSending(false);
    }
  };

  return (
    <div className="h-full bg-[#e9e8e8] text-[#111] flex flex-col" onPaste={handlePaste}>
      <div className="h-[92px] px-[18px] flex items-center">
        <div className="flex-1 h-[38px] bg-[#dcdcdc] rounded-[4px] flex items-center px-[10px]">
          <svg className="w-[18px] h-[18px] text-[#777] shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="ml-[8px] bg-transparent outline-none text-[15px] w-full placeholder-[#888]"
            placeholder="搜索群发对象"
          />
        </div>
      </div>

      <div className="px-[18px] flex flex-wrap gap-[8px] shrink-0">
        <BroadcastSelectButton label={`全选好友 ${friends.length}`} onClick={() => selectEntries(friends)} />
        <BroadcastSelectButton label={`全选群 ${groups.length}`} onClick={() => selectEntries(groups)} />
        <BroadcastSelectButton label="清空" onClick={() => setSelected(new Set())} />
      </div>

      <div className="px-[18px] py-[12px] shrink-0">
        <textarea
          ref={messageInputRef}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          className="w-full h-[94px] resize-none rounded-[4px] bg-white border border-[#d8d8d8] outline-none px-[10px] py-[8px] text-[15px]"
          placeholder="输入要群发的消息"
        />
        {broadcastImages.length > 0 && (
          <div className="mt-[8px] flex flex-wrap gap-[8px]">
            {broadcastImages.map((image) => (
              <div
                key={image.id}
                className="inline-flex items-center gap-[8px] rounded-[4px] border border-[#d8d8d8] bg-white p-[6px]"
              >
                <img src={image.preview} alt="" className="w-[54px] h-[54px] rounded-[3px] object-cover" />
                <div className="min-w-0 max-w-[160px] text-[13px] text-[#555]">
                  <div className="truncate">{image.label}</div>
                  <div className="mt-[3px] text-[#999] truncate">{image.token}</div>
                </div>
                <button
                  type="button"
                  onClick={() => removeBroadcastImage(image)}
                  className="w-[26px] h-[26px] rounded-[4px] text-[#777] active:bg-[#f2f2f2]"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="mt-[8px] flex items-center justify-between text-[13px] text-[#777]">
          <span>已选 {selected.size} 个对象{sending ? `，已发送 ${sent}，失败 ${failed}` : ""}</span>
          <button
            type="button"
            disabled={sending || selected.size === 0 || !hasPayload}
            onClick={sendBroadcast}
            className="h-[34px] min-w-[92px] rounded-[4px] bg-[#07c160] text-white disabled:bg-[#b9d9c7] active:opacity-80"
          >
            {sending ? "发送中" : "发送"}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto border-t border-[#d8d8d8]">
        {visible.map((entry) => (
          <label
            key={`${entry.source}_${entry.wxid}`}
            className="h-[60px] px-[14px] flex items-center gap-[10px] hover:bg-[#dedede] cursor-pointer"
          >
            <input
              type="checkbox"
              checked={selected.has(entry.wxid)}
              onChange={() => toggle(entry.wxid)}
              className="w-[16px] h-[16px] accent-[#07c160] shrink-0"
            />
            <EntryAvatar entry={entry} />
            <div className="min-w-0 flex-1">
              <div className="text-[16px] truncate">{entry.name || entry.wxid}</div>
              <div className="text-[12px] text-[#999] truncate">{entry.is_group ? "群聊" : "好友"} · {entry.wxid}</div>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

function BroadcastSelectButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="h-[30px] px-[10px] rounded-[4px] bg-white border border-[#d4d4d4] text-[13px] text-[#333] active:bg-[#f2f2f2]"
    >
      {label}
    </button>
  );
}

function SelfProfileCard({
  profile,
  fallbackName,
  fallbackAvatar,
  loading,
  onClose,
  onAvatarClick,
}: {
  profile?: ContactProfile;
  fallbackName: string;
  fallbackAvatar: string;
  loading: boolean;
  onClose: () => void;
  onAvatarClick: () => void;
}) {
  const raw = profile?.profile || {};
  const name = profileDisplayName(profile, fallbackName);
  const avatar = profileAvatar(profile, fallbackAvatar);
  const bigAvatar = raw.BigHeadImgUrl || raw.head_big || avatar;
  const alias = raw.Alias || raw.alias || raw.account || "";
  const area = profileArea(raw);

  return (
    <div className="fixed inset-0 z-[9998]" onClick={onClose}>
      <div
        className="absolute left-[24px] top-[86px] w-[420px] bg-white text-[#111] rounded-[2px] shadow-2xl border border-[#ddd]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-[36px] pt-[34px] pb-[28px]">
          <div className="flex gap-[24px]">
            <button
              type="button"
              className="w-[92px] h-[92px] rounded-[8px] overflow-hidden bg-[#ddd] shrink-0 active:opacity-80"
              onClick={onAvatarClick}
              title="查看大图"
            >
              {avatar ? (
                <img src={avatar} alt="" className="w-full h-full object-cover" />
              ) : (
                <div className="w-full h-full bg-[#576b95] text-white flex items-center justify-center text-[30px]">
                  {(name || "我")[0]}
                </div>
              )}
            </button>
            <div className="min-w-0 flex-1 pt-[2px]">
              <div className="flex items-center gap-[8px]">
                <h3 className="text-[24px] leading-[30px] font-medium truncate">{name}</h3>
                <svg className="w-[18px] h-[18px] text-[#1e9bf0] shrink-0" viewBox="0 0 24 24" fill="currentColor">
                  <circle cx="12" cy="7" r="4" />
                  <path d="M4.8 21c.8-4.2 3.2-6.3 7.2-6.3s6.4 2.1 7.2 6.3H4.8Z" />
                </svg>
              </div>
              <div className="mt-[7px] text-[16px] leading-[24px] text-[#888] truncate">微信号：{alias || raw.wxid || profile?.wxid || ""}</div>
              {area && <div className="text-[16px] leading-[24px] text-[#888] truncate">地区：{area}</div>}
              {loading && <div className="mt-[12px] text-[13px] text-[#999]">正在加载资料...</div>}
            </div>
          </div>
          <div className="h-px bg-[#e8e8e8] my-[26px]" />
          <button
            type="button"
            className="mx-auto w-[166px] h-[48px] rounded-[4px] bg-[#07c160] text-white text-[19px] flex items-center justify-center active:opacity-85"
            onClick={onClose}
          >
            发消息
          </button>
        </div>
        {bigAvatar && <span className="hidden">{bigAvatar}</span>}
      </div>
    </div>
  );
}

function LargeAvatarOverlay({ src, onClose }: { src: string; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[9999] bg-white flex flex-col" onClick={onClose}>
      <div className="h-[54px] shrink-0 border-b border-[#e5e5e5] flex items-center px-[18px] text-[#555]">
        <button type="button" className="w-[38px] h-[38px] flex items-center justify-center active:opacity-70" onClick={onClose}>
          <svg className="w-[24px] h-[24px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 18 9 12l6-6" />
          </svg>
        </button>
      </div>
      <div className="flex-1 min-h-0 flex items-center justify-center p-[28px]">
        {src ? (
          <img src={src} alt="" className="max-w-full max-h-full object-contain" />
        ) : (
          <div className="text-[#999]">暂无头像</div>
        )}
      </div>
    </div>
  );
}

function EmptyChatPane() {
  return (
    <div className="h-full flex items-center justify-center bg-[#111111]">
      <div className="text-[#2a2a2a]">
        <svg className="w-[128px] h-[96px]" viewBox="0 0 160 120" fill="currentColor">
          <path opacity=".42" d="M65 22c-25 0-45 16-45 36 0 12 7 23 19 29l-4 17 19-11c4 1 7 1 11 1 25 0 45-16 45-36S90 22 65 22Zm-17 32a7 7 0 1 1 0-14 7 7 0 0 1 0 14Zm34 0a7 7 0 1 1 0-14 7 7 0 0 1 0 14Z" />
          <path opacity=".28" d="M105 54c20 0 36 13 36 29 0 10-6 18-16 23l3 13-15-8c-3 .5-6 1-9 1-20 0-36-13-36-29s16-29 37-29Zm-12 25a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm27 0a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z" />
        </svg>
      </div>
    </div>
  );
}
