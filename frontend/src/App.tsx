import { useState, useCallback, useEffect, useRef, type FormEvent, type TouchEvent } from "react";
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
  getGroupMemberDetails,
  getGroupMemberNames,
  loginWithKey,
  markAsRead,
  markSessionUnread,
  multiAccountBroadcastImageUpload,
  multiAccountBroadcastText,
  muteSession,
  setActiveAgentId,
  setAccessKey,
  stickyChat,
  unmuteSession,
  unpinChat,
} from "./api";
import type { ContactProfile, Session, ChatMessage, WSMessage, WeChatAccount } from "./types";
import { replaceWechatEmojis } from "./utils/wechatEmoji";

type ViewMode = "chats" | "contacts" | "broadcast";
type MobileTab = "chats" | "contacts" | "me";
type PortalTheme = "dark" | "light";
const PORTAL_THEME_STORAGE = "wechat_web_portal_theme";
const SIDE_PANEL_WIDTH_STORAGE = "wechat_web_side_panel_width";
const PINNED_ORDER_THRESHOLD = 1_000_000_000_000;

function clampSidePanelWidth(value: number): number {
  return Math.min(460, Math.max(236, Math.round(value)));
}

function useIsMobileViewport() {
  const getValue = () => {
    if (typeof window === "undefined") return false;
    return window.innerWidth <= 768 || (window.matchMedia?.("(pointer: coarse)").matches && window.innerWidth <= 1024);
  };
  const [isMobile, setIsMobile] = useState(getValue);

  useEffect(() => {
    const update = () => setIsMobile(getValue());
    update();
    window.addEventListener("resize", update);
    window.addEventListener("orientationchange", update);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("orientationchange", update);
    };
  }, []);

  return isMobile;
}

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

function pickFirstString(...values: any[]): string {
  for (const value of values) {
    if (value === undefined || value === null) continue;
    const text = String(value);
    if (text !== "") return text;
  }
  return "";
}

function pickFirstNumber(...values: any[]): number {
  for (const value of values) {
    if (value === undefined || value === null || value === "") continue;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return 0;
}

function isTruthySessionFlag(value: any): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const text = String(value ?? "").trim().toLowerCase();
  return text === "1" || text === "true" || text === "yes";
}

function normalizeSessionOrder(value: any): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function isPinnedSessionOrder(value: any): boolean {
  return normalizeSessionOrder(value) >= PINNED_ORDER_THRESHOLD;
}

function nextSessionOrder(timestamp?: number): number {
  return timestamp || Math.floor(Date.now() / 1000);
}

function nextPinnedOrder(): number {
  return Math.max(Date.now(), PINNED_ORDER_THRESHOLD + Math.floor(Date.now() / 1000));
}

// ─── Parse sessions from the WeChat Session table snapshot ───────
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
      const wxid = pickFirstString(
        s.strUsrName,
        s.StrUsrName,
        s.UserName,
        s.userName,
        s.wxid,
      ).trim();
      if (shouldFilterSession(wxid)) return null;

      const lastMsg = lastMessages[wxid];
      let lastMsgPreview = "";
      let lastTimestamp = 0;
      const sessionContent = pickFirstString(s.strContent, s.StrContent, s.content, s.lastMsg);
      const unread = pickFirstNumber(s.nUnReadCount, s.UnReadCount, s.unread);
      const atMe = isTruthySessionFlag(s.othersAtMe ?? s.OthersAtMe ?? s.atMe);
      const nOrder = pickFirstNumber(s.nOrder, s.NOrder, s.order);
      const sessionTimestamp = pickFirstNumber(
        s.nTime,
        s.NTime,
        s.nUpdateTime,
        s.nCreateTime,
        s.CreateTime,
        s.timestamp,
        s.lastTimestamp,
      );

      if (sessionContent) {
        lastMsgPreview = replaceWechatEmojis(sessionContent);
        lastTimestamp = sessionTimestamp || 0;
      } else if (lastMsg) {
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
      if (lastMsg && (!lastTimestamp || lastMsg.time > lastTimestamp)) {
        lastTimestamp = lastMsg.time || lastTimestamp;
      }
      if (atMe && lastMsgPreview && !lastMsgPreview.startsWith("[有人@我]")) {
        lastMsgPreview = `[有人@我] ${lastMsgPreview}`;
      }

      return {
        wxid,
        nickname: nameMap[wxid] || pickFirstString(s.strNickName, s.StrNickName, s.NickName, s.nickname) || wxid,
        avatar: "",
        lastMsg: lastMsgPreview,
        lastTime: formatSessionTime(lastTimestamp),
        lastTimestamp,
        unread,
        atMe,
        muted: false,
        pinned: isTruthySessionFlag(s.pinned) || isPinnedSessionOrder(nOrder),
        order: nOrder || -index,
        is_group: wxid.includes("@chatroom"),
      } as Session;
    })
    .filter(Boolean) as Session[];
}

// ─── Build contact name map ──────────────────────────────────────
function buildContactMap(raw: any): Record<string, string> {
  const map: Record<string, string> = {};
  if (!raw) return map;
  const list = [
    ...(raw.friend || raw.friends || []),
    ...(raw.chatroom || raw.chatrooms || raw.group || raw.groups || []),
    ...(Array.isArray(raw.data) ? raw.data : []),
    ...(Array.isArray(raw) ? raw : []),
  ];
  for (const c of list) {
    const wxid = c.wxid || c.UserName || c.userName || c.strUsrName || "";
    const name = c.markname || c.nickname || c.NickName || c.strNickName || "";
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
    const list = [
      ...(contacts.friend || contacts.friends || []),
      ...(contacts.chatroom || contacts.chatrooms || contacts.group || contacts.groups || []),
      ...(Array.isArray(contacts.data) ? contacts.data : []),
      ...(Array.isArray(contacts) ? contacts : []),
    ];
    for (const c of list) {
      const wxid = c.wxid || c.UserName || c.userName || c.strUsrName || "";
      if (!wxid || map[wxid]) continue;
      const url =
        c.headimgurl || c.head_img || c.head_big || c.head_small ||
        c.headimg || c.bigheadimgurl || c.smallheadimgurl ||
        c.HeadImgUrl || c.SmallHeadImgUrl || c.BigHeadImgUrl ||
        c.HeadUrl || c.smallHeadUrl || c.bigHeadUrl ||
        c.avatar || "";
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
    raw.headimgurl ||
    raw.head_img ||
    raw.head_big ||
    raw.head_small ||
    raw.headimg ||
    raw.HeadImgUrl ||
    raw.HeadUrl ||
    raw.smallHeadUrl ||
    raw.bigHeadUrl ||
    raw.smallheadimgurl ||
    raw.bigheadimgurl ||
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

function profileField(raw: Record<string, any> | undefined, keys: string[]): string {
  if (!raw) return "";
  for (const key of keys) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function sourceLabel(source: unknown): string {
  const n = Number(source || 0);
  if (!n) return "";
  const labels: Record<number, string> = {
    1: "通过搜索QQ号添加",
    2: "通过邮箱添加",
    3: "通过搜索微信号添加",
    6: "通过单向添加",
    10: "通过朋友圈添加",
    12: "通过QQ好友添加",
    14: "通过群聊添加",
    15: "通过搜索手机号添加",
    17: "通过名片分享添加",
    30: "通过扫一扫添加",
    31: "通过Facebook添加",
  };
  return labels[n] || `来源 ${n}`;
}

const contactNameCollator = new Intl.Collator("zh-Hans-CN-u-co-pinyin", {
  sensitivity: "base",
  numeric: true,
});

function contactSortName(entry: DirectoryEntry): string {
  return (entry.name || entry.wxid || "").trim();
}

function sortDirectoryEntries(entries: DirectoryEntry[]): DirectoryEntry[] {
  return [...entries].sort((a, b) => {
    const byName = contactNameCollator.compare(contactSortName(a), contactSortName(b));
    if (byName !== 0) return byName;
    return a.wxid.localeCompare(b.wxid);
  });
}

function contactInitial(entry: DirectoryEntry): string {
  const ch = contactSortName(entry).charAt(0).toUpperCase();
  if (/^[A-Z]$/.test(ch)) return ch;
  if (/^[0-9]$/.test(ch)) return "#";
  return "#";
}

function groupDirectoryEntries(entries: DirectoryEntry[]): Array<{ title: string; entries: DirectoryEntry[] }> {
  const sorted = sortDirectoryEntries(entries);
  const groups = new Map<string, DirectoryEntry[]>();
  for (const entry of sorted) {
    const key = contactInitial(entry);
    const list = groups.get(key) || [];
    list.push(entry);
    groups.set(key, list);
  }
  const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");
  const result: Array<{ title: string; entries: DirectoryEntry[] }> = [];
  for (const letter of letters) {
    const list = groups.get(letter);
    if (list?.length) result.push({ title: letter, entries: list });
  }
  const other = groups.get("#");
  if (other?.length) result.push({ title: "#", entries: other });
  return result;
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
    const pinnedDelta = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
    if (pinnedDelta !== 0) return pinnedDelta;
    const aOrder = normalizeSessionOrder(a.order);
    const bOrder = normalizeSessionOrder(b.order);
    if (aOrder !== bOrder) return bOrder - aOrder;
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
  const isMobile = useIsMobileViewport();
  const [authenticated, setAuthenticated] = useState(() => Boolean(getAccessKey()));
  const [portalTheme, setPortalThemeState] = useState<PortalTheme>(() =>
    window.localStorage.getItem(PORTAL_THEME_STORAGE) === "light" ? "light" : "dark"
  );
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
  const [mobileTab, setMobileTab] = useState<MobileTab>("chats");
  const [mobileProfileDetailOpen, setMobileProfileDetailOpen] = useState(false);
  const [directoryProfileWxid, setDirectoryProfileWxid] = useState<string | null>(null);
  const [directoryProfileLoading, setDirectoryProfileLoading] = useState(false);
  const [sidePanelWidth, setSidePanelWidth] = useState(() => {
    const stored = Number(window.localStorage.getItem(SIDE_PANEL_WIDTH_STORAGE));
    return Number.isFinite(stored) && stored > 0 ? clampSidePanelWidth(stored) : 272;
  });

  // ─── Resolve avatars for group senders (incremental) ─────────────
  // Instead of fetching ALL group members (slow BatchGetContactBriefInfo),
  // we only resolve wxids that actually appear in loaded messages.
  const pendingBriefWxids = useRef<Set<string>>(new Set());
  const briefTimer = useRef<number | null>(null);
  const briefInFlight = useRef(false);
  const briefRequestedWxids = useRef<Set<string>>(new Set());
  const groupProfileRequestedWxids = useRef<Set<string>>(new Set());
  const groupNamesFetched = useRef<Set<string>>(new Set());
  // Keep a live ref to avatarMap so flushBriefQueue always sees the latest
  const avatarMapRef = useRef(avatarMap);
  avatarMapRef.current = avatarMap;
  const contactMapRef = useRef(contactMap);
  contactMapRef.current = contactMap;
  const contactProfilesRef = useRef(contactProfiles);
  contactProfilesRef.current = contactProfiles;
  const selectedAccountIdRef = useRef(selectedAccountId);
  selectedAccountIdRef.current = selectedAccountId;

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
    setMobileTab("chats");
    setMobileProfileDetailOpen(false);
    setDirectoryProfileWxid(null);
    setDirectoryProfileLoading(false);
    setContactsHydrating(false);
    setContactsHydrated(false);
    pendingBriefWxids.current.clear();
    briefRequestedWxids.current.clear();
    groupProfileRequestedWxids.current.clear();
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

  const setPortalTheme = useCallback((theme: PortalTheme) => {
    setPortalThemeState(theme);
    window.localStorage.setItem(PORTAL_THEME_STORAGE, theme);
  }, []);

  const startSidePanelResize = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sidePanelWidth;
    let latestWidth = startWidth;

    const handleMove = (moveEvent: PointerEvent) => {
      latestWidth = clampSidePanelWidth(startWidth + moveEvent.clientX - startX);
      setSidePanelWidth(latestWidth);
    };

    const handleUp = () => {
      window.removeEventListener("pointermove", handleMove);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.localStorage.setItem(SIDE_PANEL_WIDTH_STORAGE, String(latestWidth));
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp, { once: true });
  }, [sidePanelWidth]);

  const applyContactProfileUpdates = useCallback((updates: Record<string, ContactProfile> | undefined) => {
    if (!updates || typeof updates !== "object" || Object.keys(updates).length === 0) return;

    const nextNames: Record<string, string> = {};
    const nextAvatars: Record<string, string> = {};

    for (const [wxid, entry] of Object.entries(updates)) {
      const raw = entry?.profile || {};
      const name = entry?.name || raw.Remark || raw.remark || raw.NickName || raw.nickname || raw.strNickName || "";
      const avatar = profileAvatar(entry, "");
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

  const ensureContactProfiles = useCallback(async (
    wxids: string[],
    gid = "",
    options: { force?: boolean } = {},
  ) => {
    const requestAccountId = selectedAccountIdRef.current;
    const force = Boolean(options.force);
    const unique = Array.from(new Set((wxids || []).filter(Boolean)));
    const cached: Record<string, ContactProfile> = {};
    const missing: string[] = [];
    for (const wxid of unique) {
      const hit = contactProfilesRef.current[wxid];
      const raw = hit?.profile || {};
      const usefulKeys = Object.keys(raw).filter((key) => key !== "wxid");
      const hasUsefulProfile = usefulKeys.length > 0 && (
        !wxid.endsWith("@openim") || Boolean(raw.OpenIM || raw.OpenIMDetail || raw.openim_detail)
      );
      if (hasUsefulProfile && !force) {
        cached[wxid] = hit;
      } else {
        missing.push(wxid);
      }
    }
    if (missing.length === 0) return cached;

    const data = await getContactProfiles(missing, gid, force);
    const members = data?.members || {};
    if (selectedAccountIdRef.current !== requestAccountId) {
      return { ...cached, ...members };
    }
    applyContactProfileUpdates(members);
    return { ...cached, ...members };
  }, [applyContactProfileUpdates]);

  const ensureGroupProfiles = useCallback((wxids: string[]) => {
    const missing = Array.from(new Set((wxids || [])
      .filter((wxid) => wxid && wxid.includes("@chatroom"))
      .filter((wxid) => !avatarMapRef.current[wxid])
      .filter((wxid) => !groupProfileRequestedWxids.current.has(wxid))));
    if (missing.length === 0) return;
    missing.forEach((wxid) => groupProfileRequestedWxids.current.add(wxid));
    ensureContactProfiles(missing).catch((err) => {
      console.error("[GROUP_PROFILE]", err);
    });
  }, [ensureContactProfiles]);

  const hydrateDirectoryContacts = useCallback(async () => {
    if (contactsHydrating || contactsHydrated) return;
    const requestAccountId = selectedAccountIdRef.current;
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
      if (selectedAccountIdRef.current !== requestAccountId) return;
      setContactsHydrated(true);
    } catch (err) {
      console.error("[CONTACTS] hydrate failed:", err);
    } finally {
      if (selectedAccountIdRef.current === requestAccountId) {
        setContactsHydrating(false);
      }
    }
  }, [contactsHydrated, contactsHydrating, ensureContactProfiles, rawContacts]);

  const openSelfProfileCard = useCallback(async () => {
    if (!selfWxid) return;
    setSelfCardOpen(true);
    setSelfProfileLoading(true);
    try {
      await ensureContactProfiles([selfWxid], "", { force: true });
    } catch (err) {
      console.error("[SELF_PROFILE]", err);
    } finally {
      setSelfProfileLoading(false);
    }
  }, [ensureContactProfiles, selfWxid]);

  const openMobileSelfProfileDetail = useCallback(async () => {
    if (!selfWxid) return;
    setMobileProfileDetailOpen(true);
    setSelfProfileLoading(true);
    try {
      await ensureContactProfiles([selfWxid], "", { force: true });
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

  const switchMobileTab = useCallback((tab: MobileTab) => {
    setMobileTab(tab);
    setActiveChat(null);
    setDirectoryProfileWxid(null);
    setMobileProfileDetailOpen(false);
    if (tab === "contacts") {
      hydrateDirectoryContacts();
    }
  }, [hydrateDirectoryContacts]);

  const openDirectoryProfile = useCallback(async (entry: DirectoryEntry) => {
    if (!entry?.wxid) return;
    setViewMode("contacts");
    setActiveChat(null);
    setDirectoryProfileWxid(entry.wxid);
    setDirectoryProfileLoading(true);
    try {
      await ensureContactProfiles([entry.wxid]);
    } catch (err) {
      console.error("[DIRECTORY_PROFILE]", err);
    } finally {
      setDirectoryProfileLoading(false);
    }
  }, [ensureContactProfiles]);

  const flushBriefQueue = useCallback(() => {
    if (briefInFlight.current) return;

    const wxids = Array.from(pendingBriefWxids.current);
    pendingBriefWxids.current.clear();
    if (wxids.length === 0) return;

    briefInFlight.current = true;
    const requestAccountId = selectedAccountIdRef.current;
    batchGetContactBrief(wxids)
      .then((data: any) => {
        if (selectedAccountIdRef.current !== requestAccountId) return;
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
      if (wxid.endsWith("@openim")) continue;
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

  const hydrateGroupSenders = useCallback((groupId: string, wxids: string[], mySelfWxid?: string) => {
    const unique = Array.from(new Set((wxids || []).filter(Boolean)));
    if (unique.length === 0) return;
    const openimWxids = unique.filter((wxid) => wxid.endsWith("@openim"));
    const regularWxids = unique.filter((wxid) => !wxid.endsWith("@openim"));
    if (regularWxids.length > 0) {
      queueBriefLookup(regularWxids, mySelfWxid);
    }
    if (openimWxids.length > 0) {
      ensureContactProfiles(openimWxids, groupId).catch((err) => {
        console.error("[OPENIM_PROFILE]", err);
      });
    }
  }, [ensureContactProfiles, queueBriefLookup]);

  // ─── Fetch group member names (fast) on entering a group ──────────
  const fetchGroupMemberNames = useCallback((gid: string) => {
    if (groupNamesFetched.current.has(gid)) return;
    groupNamesFetched.current.add(gid);
    const requestAccountId = selectedAccountIdRef.current;

    getGroupMemberNames(gid)
      .then((data: any) => {
        if (selectedAccountIdRef.current !== requestAccountId) return;
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
        contact_profiles,
        messages_cache,
        session_cache,
      } = wsMsg.data as any;
      const wxid = self_info?.data?.wxid || self_info?.wxid || "";
      setSelfWxid(wxid);
      setRawContacts(contacts);

      const nameMap = buildContactMap(contacts);
      const avatars = buildAvatarMap(contacts, avatar_urls);
      const cachedProfiles = (contact_profiles && typeof contact_profiles === "object") ? contact_profiles : {};
      for (const [profileWxid, entry] of Object.entries<ContactProfile>(cachedProfiles)) {
        const raw = entry?.profile || {};
        const name = entry?.name || raw.Remark || raw.remark || raw.NickName || raw.nickname || raw.strNickName || "";
        const avatar = profileAvatar(entry, "");
        if (name && name !== profileWxid) nameMap[profileWxid] = name;
        if (avatar) avatars[profileWxid] = avatar;
      }
      setContactMap(nameMap);
      setAvatarMap(avatars);
      if (Object.keys(cachedProfiles).length > 0) {
        setContactProfiles((prev) => ({ ...prev, ...cachedProfiles }));
      }

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
            pinned: Boolean(current.pinned || snap?.pinned),
            order: normalizeSessionOrder(current.order || snap?.order),
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
            pinned: Boolean(snap?.pinned),
            order: nextSessionOrder(snapTs),
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
      const openimNeedsProfile = needsBrief.filter((targetWxid) => targetWxid.endsWith("@openim"));
      const regularNeedsBrief = needsBrief.filter((targetWxid) => !targetWxid.endsWith("@openim"));
      queueBriefLookup(regularNeedsBrief, wxid);
      if (openimNeedsProfile.length > 0) {
        ensureContactProfiles(openimNeedsProfile).catch((err) => console.error("[OPENIM_PROFILE]", err));
      }
      const groupNeedsProfile = enriched
        .filter((s) => s.is_group && !avatars[s.wxid])
        .map((s) => s.wxid);
      ensureGroupProfiles(groupNeedsProfile);

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

    if (wsMsg.type === "contact_profiles") {
      const members = (wsMsg.data as any)?.members || {};
      applyContactProfileUpdates(members);
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
        const avatar = profileAvatar(entry, "");
        if (name && name !== wxid) liveContactMap[wxid] = name;
        if (avatar) liveAvatarMap[wxid] = avatar;
      }

      const groupSenderWxidsByChat = new Map<string, Set<string>>();
      const groupChatWxidsNeedingProfile = new Set<string>();
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
          if (!liveAvatarMap[chatId]) groupChatWxidsNeedingProfile.add(chatId);
          const senderWxid = String(chatMsg.fromid || "");
          if (senderWxid && senderWxid !== myWxid) {
            const existing = groupSenderWxidsByChat.get(chatId) || new Set<string>();
            existing.add(senderWxid);
            groupSenderWxidsByChat.set(chatId, existing);
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
                  order: prev[idx].pinned ? prev[idx].order : Math.max(normalizeSessionOrder(prev[idx].order), nextSessionOrder(msgTs)),
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
                  pinned: false,
                  order: nextSessionOrder(msgTs),
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

      for (const [chatId, senderWxids] of groupSenderWxidsByChat.entries()) {
        hydrateGroupSenders(chatId, Array.from(senderWxids), myWxid || selfWxid);
      }
      if (groupChatWxidsNeedingProfile.size > 0) {
        ensureGroupProfiles(Array.from(groupChatWxidsNeedingProfile));
      }
      if (directChatWxids.size > 0) {
        const directWxids = Array.from(directChatWxids);
        const directOpenimWxids = directWxids.filter((wxid) => wxid.endsWith("@openim"));
        const directRegularWxids = directWxids.filter((wxid) => !wxid.endsWith("@openim"));
        queueBriefLookup(directRegularWxids, myWxid || selfWxid);
        if (directOpenimWxids.length > 0) {
          ensureContactProfiles(directOpenimWxids).catch((err) => console.error("[OPENIM_PROFILE]", err));
        }
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
          ? {
              ...prev[idx],
              lastMsg: preview,
              lastTime: timeStr,
              lastTimestamp: sentTs,
              unread: 0,
              order: prev[idx].pinned ? prev[idx].order : Math.max(normalizeSessionOrder(prev[idx].order), nextSessionOrder(sentTs)),
            }
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
              pinned: false,
              order: nextSessionOrder(sentTs),
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
  }, [selfWxid, activeChat, contactMap, avatarMap, queueBriefLookup, hydrateGroupSenders, ensureContactProfiles, ensureGroupProfiles, applyContactProfileUpdates, selectedAccountId]);

  const { connected } = useWebSocket(handleWSMessage, authenticated && Boolean(selectedAccountId));

  useEffect(() => {
    if (!authenticated || selectedAccountId) return;
    loadAccounts();
    const timer = window.setInterval(loadAccounts, 3000);
    return () => window.clearInterval(timer);
  }, [authenticated, selectedAccountId, loadAccounts]);

  // ─── Navigation (browser history integration for mobile back gesture) ──
  const handleSelectChat = (wxid: string, seed?: Partial<Session>) => {
    setViewMode("chats");
    setDirectoryProfileWxid(null);
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.wxid === wxid);
      if (idx >= 0 && !seed) return prev;
      const nowTs = Math.floor(Date.now() / 1000);
      if (idx >= 0) {
        const existing = prev[idx];
        const updated: Session = {
          ...existing,
          nickname: seed?.nickname || existing.nickname || contactMapRef.current[wxid] || wxid,
          avatar: seed?.avatar || existing.avatar || avatarMapRef.current[wxid] || "",
          is_group: Boolean(seed?.is_group ?? existing.is_group ?? wxid.includes("@chatroom")),
          lastTimestamp: existing.lastTimestamp || nowTs,
          lastTime: existing.lastTime || formatSessionTime(nowTs),
          unread: 0,
          order: existing.pinned ? existing.order : Math.max(normalizeSessionOrder(existing.order), nextSessionOrder(nowTs)),
        };
        const rest = prev.filter((s) => s.wxid !== wxid);
        return sortSessionsForDisplay([updated, ...rest]);
      }
      const seeded: Session = {
        wxid,
        nickname: seed?.nickname || contactMapRef.current[wxid] || wxid,
        avatar: seed?.avatar || avatarMapRef.current[wxid] || "",
        is_group: Boolean(seed?.is_group ?? wxid.includes("@chatroom")),
        lastMsg: "",
        lastTime: formatSessionTime(nowTs),
        lastTimestamp: nowTs,
        unread: 0,
        muted: false,
        pinned: false,
        order: nextSessionOrder(nowTs),
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
          hydrateGroupSenders(wxid, senderWxids, selfWxid);
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
          s.wxid === wxid ? { ...s, pinned: true, order: Math.max(normalizeSessionOrder(s.order), nextPinnedOrder()) } : s
        )));
        return;
      }
      if (action === "unpin") {
        await unpinChat(wxid);
        setSessions((prev) => sortSessionsForDisplay(prev.map((s) =>
          s.wxid === wxid ? { ...s, pinned: false, order: nextSessionOrder(s.lastTimestamp) } : s
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
        const updated = {
          ...existing,
          lastMsg: preview,
          lastTime: timeStr,
          lastTimestamp: msgTs,
          order: existing.pinned ? existing.order : Math.max(normalizeSessionOrder(existing.order), nextSessionOrder(msgTs)),
        };
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
        hydrateGroupSenders(wxid, Array.from(senderWxids), selfWxid);
      }
    }
  };

  const friendEntries: DirectoryEntry[] = sortDirectoryEntries(contactListFromRaw(rawContacts)
    .map((c: any) => {
      const wxid = c.wxid || c.UserName || c.userName || c.strUsrName || "";
      if (!wxid || shouldFilterSession(wxid)) return null;
      const profile = contactProfiles[wxid];
      const fallbackName = c.markname || c.nickname || c.NickName || c.strNickName || contactMap[wxid] || wxid;
      const fallbackAvatar =
        avatarMap[wxid] ||
        c.smallhead ||
        c.bighead ||
        c.headimgurl ||
        c.head_img ||
        c.head_big ||
        c.head_small ||
        c.SmallHeadImgUrl ||
        c.BigHeadImgUrl ||
        c.HeadImgUrl ||
        c.HeadUrl ||
        c.smallHeadUrl ||
        c.bigHeadUrl ||
        c.avatar ||
        "";
      return {
        wxid,
        name: profileDisplayName(profile, fallbackName),
        avatar: profileAvatar(profile, fallbackAvatar),
        is_group: false,
        source: "friend" as const,
      };
    })
    .filter(Boolean) as DirectoryEntry[]);

  const rawRoomEntries = chatroomListFromRaw(rawContacts)
    .map((c: any) => ({
      wxid: c.wxid || c.UserName || c.userName || c.strUsrName || "",
      name: c.nickname || c.NickName || c.strNickName || "",
      avatar: c.smallhead || c.bighead || c.SmallHeadImgUrl || c.BigHeadImgUrl ||
        c.headimgurl || c.head_img || c.head_big || c.head_small ||
        c.HeadImgUrl || c.HeadUrl || c.smallHeadUrl || c.bigHeadUrl || c.avatar || "",
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
  const groupEntries = sortDirectoryEntries(Array.from(groupEntryMap.values()));
  const directoryEntryMap = new Map<string, DirectoryEntry>();
  for (const entry of [...friendEntries, ...groupEntries]) {
    directoryEntryMap.set(entry.wxid, entry);
  }
  const directoryProfileEntry = directoryProfileWxid ? directoryEntryMap.get(directoryProfileWxid) || null : null;
  const selfProfile = selfWxid ? contactProfiles[selfWxid] : undefined;
  const selfInfoName = contactMap[selfWxid] || (selfProfile ? profileDisplayName(selfProfile, "") : "") || "我";
  const selfAvatar =
    profileAvatar(selfProfile, avatarMap[selfWxid] || "") ||
    (selfProfile?.profile?.BigHeadImgUrl || selfProfile?.profile?.SmallHeadImgUrl || "");
  const darkTheme = portalTheme === "dark";
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
    if (isMobile) {
      return (
        <MobileAccountPortal
          accounts={accounts}
          loading={accountsLoading}
          theme={portalTheme}
          onThemeChange={setPortalTheme}
          onRefresh={loadAccounts}
          onSelectAccount={handleSelectAccount}
          onLogout={handleLogout}
        />
      );
    }
    return (
      <AccountPortal
        accounts={accounts}
        loading={accountsLoading}
        theme={portalTheme}
        onThemeChange={setPortalTheme}
        onRefresh={loadAccounts}
        onSelectAccount={handleSelectAccount}
        onLogout={handleLogout}
      />
    );
  }

  if (isMobile) {
    if (activeChat && activeSession) {
      return (
        <div className={`h-dvh w-screen overflow-hidden ${darkTheme ? "bg-[#111111]" : "bg-[#ededed]"}`}>
          {!connected && (
            <div className="fixed top-0 left-0 right-0 bg-[#e6a23c] text-black text-center text-[12px] py-1 z-50">
              正在连接后端服务器...
            </div>
          )}
          <ChatArea
            mobile
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
            dark={darkTheme}
          />
        </div>
      );
    }

    if (mobileProfileDetailOpen) {
      return (
        <>
          <MobileProfileDetailPage
            profile={selfProfile}
            fallbackName={selfInfoName}
            fallbackAvatar={selfAvatar}
            loading={selfProfileLoading}
            onBack={() => setMobileProfileDetailOpen(false)}
            onAvatarClick={() => setSelfImageOpen(true)}
            dark={darkTheme}
          />
          {selfImageOpen && (
            <LargeAvatarOverlay
              src={
                selfProfile?.profile?.BigHeadImgUrl ||
                selfProfile?.profile?.head_big ||
                selfAvatar
              }
              onClose={() => setSelfImageOpen(false)}
              dark={darkTheme}
            />
          )}
        </>
      );
    }

    if (directoryProfileEntry) {
      return (
        <MobileDirectoryProfilePage
          entry={directoryProfileEntry}
          profile={contactProfiles[directoryProfileEntry.wxid]}
          loading={directoryProfileLoading}
          dark={darkTheme}
          onBack={() => setDirectoryProfileWxid(null)}
          onMessage={() => handleSelectChat(directoryProfileEntry.wxid, {
            nickname: directoryProfileEntry.name,
            avatar: directoryProfileEntry.avatar,
            is_group: directoryProfileEntry.is_group,
          })}
        />
      );
    }

    return (
      <MobileMainShell
        tab={mobileTab}
        sessions={sessions}
        friends={friendEntries}
        groups={groupEntries}
        selfName={selfInfoName}
        selfWxid={selfWxid}
        selfAvatar={selfAvatar}
        selfProfile={selfProfile}
        contactsLoading={contactsHydrating}
        dark={darkTheme}
        onSwitchTab={switchMobileTab}
        onSelectChat={handleSelectChat}
        onSelectContact={openDirectoryProfile}
        onHydrateContacts={hydrateDirectoryContacts}
        onOpenSelfDetail={openMobileSelfProfileDetail}
        onBackToAccounts={handleLeaveAccount}
      />
    );
  }

  return (
    <div className={`h-dvh w-screen overflow-hidden relative flex ${darkTheme ? "bg-[#111111]" : "bg-[#f5f5f5]"}`}>
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

      <div
        className={`relative shrink-0 border-r h-full ${darkTheme ? "border-[#2a2a2a] bg-[#191919]" : "border-[#d8d8d8] bg-[#e9e8e8]"}`}
        style={{ width: sidePanelWidth }}
      >
        {viewMode === "chats" && (
          <SessionList
            sessions={sessions}
            activeWxid={activeChat}
            onSelectChat={handleSelectChat}
            onSessionAction={handleSessionMenuAction}
            theme={portalTheme}
          />
        )}
        {viewMode === "contacts" && (
          <ContactsPanel
            friends={friendEntries}
            groups={groupEntries}
            loading={contactsHydrating}
            dark={darkTheme}
            onHydrate={hydrateDirectoryContacts}
            onSelect={openDirectoryProfile}
          />
        )}
        {viewMode === "broadcast" && (
          <BroadcastPanel
            friends={friendEntries}
            groups={groupEntries}
            dark={darkTheme}
          />
        )}
        <div
          role="separator"
          aria-label="调整列表宽度"
          aria-orientation="vertical"
          onPointerDown={startSidePanelResize}
          className="absolute top-0 right-[-2px] z-30 h-full w-[4px] cursor-col-resize flex justify-center group"
        >
          <div className={`h-full w-px transition-colors ${
            darkTheme ? "bg-[#2a2a2a] group-hover:bg-[#3a3a3a]" : "bg-[#d0d0d0] group-hover:bg-[#bdbdbd]"
          }`} />
        </div>
      </div>

      <div className={`flex-1 min-w-0 h-full ${darkTheme ? "bg-[#111111]" : "bg-[#ededed]"}`}>
        {viewMode === "contacts" && directoryProfileEntry ? (
          <DirectoryProfilePane
            entry={directoryProfileEntry}
            profile={contactProfiles[directoryProfileEntry.wxid]}
            fallbackAvatar={directoryProfileEntry.avatar}
            loading={directoryProfileLoading}
            dark={darkTheme}
            onMessage={() => handleSelectChat(directoryProfileEntry.wxid, {
              nickname: directoryProfileEntry.name,
              avatar: directoryProfileEntry.avatar,
              is_group: directoryProfileEntry.is_group,
            })}
          />
        ) : activeChat && activeSession ? (
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
            dark={darkTheme}
          />
        ) : (
          <EmptyChatPane dark={darkTheme} />
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
          dark={darkTheme}
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
          dark={darkTheme}
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

function accountStatusMeta(account: WeChatAccount, dark: boolean): { text: string; className: string } {
  const status = String(account.login_status || "");
  const message = String(account.login_message || "");
  const success = dark ? "bg-[#123d27] text-[#49d17d]" : "bg-[#e5f7ed] text-[#078f49]";
  const warning = dark ? "bg-[#3d3112] text-[#e6bd51]" : "bg-[#fff3d9] text-[#9a6b00]";
  const neutral = dark ? "bg-[#252525] text-[#aaa]" : "bg-[#f0f0f0] text-[#666]";

  if (status === "5") return { text: message || "点击进入微信", className: warning };
  if (status === "2") return { text: message || "正在登录中...", className: warning };
  if (status === "3") {
    return account.initialized
      ? { text: "已就绪", className: success }
      : { text: "初始化中", className: warning };
  }
  if (account.initialized) return { text: "已就绪", className: success };
  return { text: message || "等待登录", className: neutral };
}

function AccountPortal({
  accounts,
  loading,
  theme,
  onThemeChange,
  onRefresh,
  onSelectAccount,
  onLogout,
}: {
  accounts: WeChatAccount[];
  loading: boolean;
  theme: PortalTheme;
  onThemeChange: (theme: PortalTheme) => void;
  onRefresh: () => void;
  onSelectAccount: (account: WeChatAccount) => void;
  onLogout: () => void;
}) {
  const dark = theme === "dark";
  const accountDisplayName = (account: WeChatAccount) =>
    (account.nickname && account.nickname !== account.id ? account.nickname : "") ||
    account.wxid ||
    account.account_id ||
    "微信";

  return (
    <div className={`h-dvh w-screen overflow-hidden flex ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#f4f4f4] text-[#111]"}`}>
      <div className={`w-[420px] max-w-[44vw] min-w-[340px] border-r h-full flex flex-col ${dark ? "border-[#2b2b2b]" : "border-[#d9d9d9]"}`}>
        <div className={`h-[96px] px-[24px] flex items-center justify-between border-b ${dark ? "border-[#242424]" : "border-[#e0e0e0]"}`}>
          <div>
            <div className="text-[22px] font-medium">微信账号</div>
            <div className={`text-[12px] mt-[3px] ${dark ? "text-[#777]" : "text-[#888]"}`}>连接 {accounts.length} 个</div>
            <div className="mt-[9px]">
              <ThemeSwitch theme={theme} onChange={onThemeChange} />
            </div>
          </div>
          <div className="flex items-center gap-[8px]">
            <button
              type="button"
              onClick={onRefresh}
              className={`h-[32px] px-[10px] rounded-[4px] active:opacity-85 ${dark ? "bg-[#242424] text-[#cfcfcf]" : "bg-white text-[#333] border border-[#d8d8d8]"}`}
            >
              刷新
            </button>
            <button
              type="button"
              onClick={onLogout}
              className={`h-[32px] px-[10px] rounded-[4px] active:opacity-85 ${dark ? "bg-[#242424] text-[#cfcfcf]" : "bg-white text-[#333] border border-[#d8d8d8]"}`}
            >
              退出
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-[18px]">
          {loading && accounts.length === 0 && <div className="text-[#777] text-[14px]">正在读取微信连接...</div>}
          {accounts.length === 0 && !loading && (
            <div className="text-[#777] text-[14px] leading-[24px]">
              暂无连接的微信。请让客户端 DLL 连接到当前后端 `/agent`。
            </div>
          )}
          <div className="space-y-[12px]">
            {accounts.map((account) => {
              const meta = accountStatusMeta(account, dark);
              return (
            <button
                key={account.id}
                type="button"
                onClick={() => onSelectAccount(account)}
                className={`w-full min-h-[100px] rounded-[6px] border p-[14px] flex items-center gap-[14px] text-left active:opacity-90 ${
                  dark ? "bg-[#1b1b1b] hover:bg-[#242424] border-[#2b2b2b]" : "bg-white hover:bg-[#f7f7f7] border-[#e3e3e3]"
                }`}
              >
                <AccountAvatar account={account} />
                <div className="min-w-0 flex-1">
                  <div className="text-[18px] truncate">{accountDisplayName(account)}</div>
                  <div className={`text-[12px] truncate mt-[5px] ${dark ? "text-[#888]" : "text-[#777]"}`}>{account.wxid || account.account_id || account.id}</div>
                  <div className={`text-[12px] truncate mt-[3px] ${dark ? "text-[#666]" : "text-[#999]"}`}>{account.peer || "connected"}</div>
                  <div className={`text-[11px] truncate mt-[3px] ${dark ? "text-[#555]" : "text-[#aaa]"}`} title={account.id}>WS {account.id}</div>
                </div>
                <div className={`text-[12px] px-[7px] py-[3px] rounded-[4px] ${meta.className}`}>
                  {meta.text}
                </div>
              </button>
              );
            })}
          </div>
        </div>
      </div>
      <div className="flex-1 min-w-0 h-full">
        <MultiAccountBroadcastPanel accounts={accounts} theme={theme} />
      </div>
    </div>
  );
}

function ThemeSwitch({ theme, onChange }: { theme: PortalTheme; onChange: (theme: PortalTheme) => void }) {
  const dark = theme === "dark";
  return (
    <button
      type="button"
      onClick={() => onChange(dark ? "light" : "dark")}
      className={`relative w-[54px] h-[28px] rounded-full p-[2px] flex items-center transition-colors ${
        dark ? "bg-[#242424] border border-[#333]" : "bg-[#e8e8e8] border border-[#d2d2d2]"
      }`}
      aria-label="切换日夜模式"
      title={dark ? "夜晚模式" : "白天模式"}
    >
      <svg className={`absolute left-[7px] w-[13px] h-[13px] ${dark ? "text-[#777]" : "text-[#f0b429]"}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="4" />
        <path strokeLinecap="round" d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
      </svg>
      <svg className={`absolute right-[7px] w-[13px] h-[13px] ${dark ? "text-[#e5e7eb]" : "text-[#999]"}`} fill="currentColor" viewBox="0 0 24 24">
        <path d="M21 14.4A7.7 7.7 0 0 1 9.6 3a8.8 8.8 0 1 0 11.4 11.4Z" />
      </svg>
      <span
        className={`relative z-10 h-[22px] w-[22px] rounded-full flex items-center justify-center shadow-sm transition-transform ${
          dark ? "translate-x-[26px] bg-[#07c160] text-white" : "translate-x-0 bg-white text-[#f0a500]"
        }`}
      >
        {dark ? (
          <svg className="w-[13px] h-[13px]" fill="currentColor" viewBox="0 0 24 24">
            <path d="M21 14.4A7.7 7.7 0 0 1 9.6 3a8.8 8.8 0 1 0 11.4 11.4Z" />
          </svg>
        ) : (
          <svg className="w-[13px] h-[13px]" fill="none" stroke="currentColor" strokeWidth={2.3} viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="4" />
            <path strokeLinecap="round" d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
          </svg>
        )}
      </span>
    </button>
  );
}

function AccountAvatar({ account }: { account: WeChatAccount }) {
  const [failed, setFailed] = useState(false);
  const name = (account.nickname && account.nickname !== account.id ? account.nickname : "") || account.wxid || account.account_id || "?";
  if (account.avatar && !failed) {
    return <img src={account.avatar} alt="" className="w-[54px] h-[54px] rounded-[5px] object-cover bg-[#333]" onError={() => setFailed(true)} />;
  }
  return (
    <div className="w-[54px] h-[54px] rounded-[5px] bg-[#07c160] text-white flex items-center justify-center text-[22px] shrink-0">
      {name[0]}
    </div>
  );
}

function MobileAccountPortal({
  accounts,
  loading,
  theme,
  onThemeChange,
  onRefresh,
  onSelectAccount,
  onLogout,
}: {
  accounts: WeChatAccount[];
  loading: boolean;
  theme: PortalTheme;
  onThemeChange: (theme: PortalTheme) => void;
  onRefresh: () => void;
  onSelectAccount: (account: WeChatAccount) => void;
  onLogout: () => void;
}) {
  const [showBroadcast, setShowBroadcast] = useState(false);
  const dark = theme === "dark";
  const statusMeta = (account: WeChatAccount) => accountStatusMeta(account, dark);
  const displayName = (account: WeChatAccount) =>
    (account.nickname && account.nickname !== account.id ? account.nickname : "") ||
    account.wxid ||
    account.account_id ||
    "微信";

  if (showBroadcast) {
    return (
      <MobileMultiAccountBroadcastPage
        accounts={accounts}
        theme={theme}
        onBack={() => setShowBroadcast(false)}
      />
    );
  }

  return (
    <div className={`h-dvh w-screen overflow-hidden flex flex-col ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#ededed] text-[#111]"}`}>
      <MobileTopBar dark={dark} title="微信账号" rightLabel="刷新" onRight={onRefresh} leftLabel="退出" onLeft={onLogout} />
      <div className={`px-[14px] py-[12px] border-b flex items-center justify-between ${dark ? "border-[#242424]" : "border-[#dedede]"}`}>
        <button
          type="button"
          onClick={() => setShowBroadcast(true)}
          className="h-[36px] px-[14px] rounded-full bg-[#07c160] text-white text-[14px] active:opacity-85"
        >
          多号群发
        </button>
        <ThemeSwitch theme={theme} onChange={onThemeChange} />
      </div>
      <div className="flex-1 overflow-y-auto px-[14px] py-[14px] pb-[calc(18px+env(safe-area-inset-bottom))]">
        {loading && accounts.length === 0 && <div className="text-center text-[#888] text-[14px] mt-[40px]">正在读取微信连接...</div>}
        {!loading && accounts.length === 0 && (
          <div className="text-center text-[#888] text-[14px] leading-[24px] mt-[44px]">
            暂无连接的微信<br />请让客户端 DLL 连接到当前后端 `/agent`
          </div>
        )}
        <div className="space-y-[12px]">
          {accounts.map((account) => {
            const meta = statusMeta(account);
            return (
            <button
              key={account.id}
              type="button"
              onClick={() => onSelectAccount(account)}
              className={`w-full min-h-[86px] rounded-[12px] shadow-sm px-[14px] py-[12px] flex items-center gap-[12px] text-left active:opacity-90 ${
                dark ? "bg-[#1b1b1b]" : "bg-white active:bg-[#f4f4f4]"
              }`}
            >
              <AccountAvatar account={account} />
              <div className="min-w-0 flex-1">
                <div className="text-[18px] font-medium truncate">{displayName(account)}</div>
                <div className={`text-[13px] truncate mt-[4px] ${dark ? "text-[#888]" : "text-[#888]"}`}>{account.wxid || account.account_id || account.id}</div>
                <div className={`text-[11px] truncate mt-[3px] ${dark ? "text-[#666]" : "text-[#aaa]"}`}>WS {account.id}</div>
              </div>
              <span className={`text-[12px] px-[7px] py-[3px] rounded-full ${meta.className}`}>
                {meta.text}
              </span>
            </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function MobileMultiAccountBroadcastPage({
  accounts,
  theme,
  onBack,
}: {
  accounts: WeChatAccount[];
  theme: PortalTheme;
  onBack: () => void;
}) {
  const dark = theme === "dark";
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(() => new Set(accounts.map((a) => a.id).filter(Boolean)));
  const [targetTypes, setTargetTypes] = useState<Set<string>>(new Set(["friends"]));
  const [message, setMessage] = useState("");
  const [image, setImage] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
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

  const displayName = (account: WeChatAccount) =>
    (account.nickname && account.nickname !== account.id ? account.nickname : "") ||
    account.wxid ||
    account.account_id ||
    "微信";
  const agentIds = Array.from(selectedAgents).filter(Boolean);
  const selectedTargetTypes = Array.from(targetTypes);

  const toggleAgent = (id: string) => {
    setSelectedAgents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleTargetType = (type: string) => {
    setTargetTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const sendText = async () => {
    if (!message.trim() || agentIds.length === 0 || selectedTargetTypes.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastText(agentIds, selectedTargetTypes, message.trim());
      setResultText(`文本完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  const sendImage = async () => {
    if (!image || agentIds.length === 0 || selectedTargetTypes.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastImageUpload(agentIds, selectedTargetTypes, image);
      setResultText(`图片完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={`h-dvh w-screen overflow-hidden flex flex-col ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#ededed] text-[#111]"}`}>
      <MobileTopBar dark={dark} title="多号群发" leftLabel="返回" rightLabel="全选" onLeft={onBack} onRight={() => setSelectedAgents(new Set(accounts.map((a) => a.id).filter(Boolean)))} />
      <div className="flex-1 overflow-y-auto px-[14px] py-[14px] pb-[calc(20px+env(safe-area-inset-bottom))]">
        <div className={`rounded-[14px] p-[12px] ${dark ? "bg-[#1b1b1b]" : "bg-white"}`}>
          <div className="flex items-center justify-between mb-[10px]">
            <div className="text-[15px] font-medium">发送账号</div>
            <button
              type="button"
              onClick={() => setSelectedAgents(new Set())}
              className={`text-[13px] ${dark ? "text-[#aaa]" : "text-[#666]"}`}
            >
              清空
            </button>
          </div>
          <div className="space-y-[8px]">
            {accounts.map((account) => {
              const checked = selectedAgents.has(account.id);
              return (
                <button
                  key={account.id}
                  type="button"
                  onClick={() => toggleAgent(account.id)}
                  className={`w-full h-[60px] rounded-[10px] px-[10px] flex items-center gap-[10px] text-left border ${
                    checked
                      ? (dark ? "bg-[#123d27] border-[#123d27]" : "bg-[#e9f8ef] border-[#07c160]")
                      : (dark ? "bg-[#242424] border-transparent" : "bg-[#f6f6f6] border-transparent")
                  }`}
                >
                  <span className={`w-[22px] h-[22px] rounded-full border flex items-center justify-center shrink-0 ${
                    checked ? "bg-[#07c160] border-[#07c160]" : (dark ? "border-[#777]" : "border-[#aaa]")
                  }`}>
                    {checked && (
                      <svg className="w-[15px] h-[15px] text-white" fill="none" stroke="currentColor" strokeWidth={2.3} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
                      </svg>
                    )}
                  </span>
                  <AccountAvatar account={account} />
                  <div className="min-w-0 flex-1">
                    <div className="text-[16px] truncate">{displayName(account)}</div>
                    <div className={`text-[12px] truncate mt-[3px] ${dark ? "text-[#888]" : "text-[#888]"}`}>{account.wxid || account.id}</div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        <div className={`mt-[12px] rounded-[14px] p-[12px] ${dark ? "bg-[#1b1b1b]" : "bg-white"}`}>
          <div className="text-[15px] font-medium mb-[10px]">目标类型</div>
          <div className="grid grid-cols-2 gap-[10px]">
            <TargetTypeButton active={targetTypes.has("friends")} dark={dark} title="所有个人" subtitle="按账号展开" onClick={() => toggleTargetType("friends")} />
            <TargetTypeButton active={targetTypes.has("groups")} dark={dark} title="所有群" subtitle="按账号展开" onClick={() => toggleTargetType("groups")} />
          </div>
        </div>

        <div className={`mt-[12px] rounded-[14px] p-[12px] ${dark ? "bg-[#1b1b1b]" : "bg-white"}`}>
          <div className="text-[15px] font-medium mb-[10px]">文本消息</div>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            className={`w-full h-[106px] resize-none rounded-[10px] border outline-none px-[10px] py-[8px] text-[15px] ${
              dark ? "bg-[#242424] border-[#333] text-[#eee]" : "bg-[#f7f7f7] border-[#e0e0e0]"
            }`}
            placeholder="输入文本"
          />
          <button
            type="button"
            disabled={sending || !message.trim() || agentIds.length === 0 || selectedTargetTypes.length === 0}
            onClick={sendText}
            className={`mt-[10px] w-full h-[42px] rounded-[10px] bg-[#07c160] text-white ${dark ? "disabled:bg-[#315541]" : "disabled:bg-[#b9d9c7]"}`}
          >
            {sending ? "发送中" : "群发文本"}
          </button>
        </div>

        <div className={`mt-[12px] rounded-[14px] p-[12px] ${dark ? "bg-[#1b1b1b]" : "bg-white"}`}>
          <div className="text-[15px] font-medium mb-[10px]">图片消息</div>
          <label className={`h-[42px] rounded-[10px] flex items-center justify-center border ${dark ? "border-[#333] bg-[#242424]" : "border-[#e0e0e0] bg-[#f7f7f7]"}`}>
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => setImage(e.target.files?.[0] || null)}
            />
            选择图片
          </label>
          {preview && <img src={preview} alt="" className={`mt-[10px] max-h-[180px] rounded-[10px] object-contain mx-auto ${dark ? "bg-black/20" : "bg-[#f7f7f7] border border-[#e0e0e0]"}`} />}
          <button
            type="button"
            disabled={sending || !image || agentIds.length === 0 || selectedTargetTypes.length === 0}
            onClick={sendImage}
            className={`mt-[10px] w-full h-[42px] rounded-[10px] bg-[#07c160] text-white ${dark ? "disabled:bg-[#315541]" : "disabled:bg-[#b9d9c7]"}`}
          >
            {sending ? "发送中" : "群发图片"}
          </button>
        </div>

        <div className={`mt-[12px] text-[13px] ${dark ? "text-[#888]" : "text-[#777]"}`}>
          已选账号 {agentIds.length} 个，目标类型 {selectedTargetTypes.length} 个。{resultText}
        </div>
      </div>
    </div>
  );
}

function MobileTopBar({
  title,
  leftLabel,
  rightLabel,
  onLeft,
  onRight,
  dark = false,
}: {
  title: string;
  leftLabel?: string;
  rightLabel?: string;
  onLeft?: () => void;
  onRight?: () => void;
  dark?: boolean;
}) {
  return (
    <div className={`shrink-0 border-b pt-[env(safe-area-inset-top)] ${dark ? "bg-[#111111] border-[#242424] text-[#e8e8e8]" : "bg-[#ededed] border-[#dedede]"}`}>
      <div className="h-[54px] px-[14px] grid grid-cols-[72px_1fr_72px] items-center">
        <button type="button" onClick={onLeft} className={`text-left text-[15px] active:opacity-60 ${dark ? "text-[#d0d0d0]" : "text-[#333]"}`}>
          {leftLabel || ""}
        </button>
        <div className="text-center text-[17px] font-semibold truncate">{title}</div>
        <button type="button" onClick={onRight} className={`text-right text-[15px] active:opacity-60 ${dark ? "text-[#d0d0d0]" : "text-[#333]"}`}>
          {rightLabel || ""}
        </button>
      </div>
    </div>
  );
}

function MobileMainShell({
  tab,
  sessions,
  friends,
  groups,
  selfName,
  selfWxid,
  selfAvatar,
  selfProfile,
  contactsLoading,
  dark,
  onSwitchTab,
  onSelectChat,
  onSelectContact,
  onHydrateContacts,
  onOpenSelfDetail,
  onBackToAccounts,
}: {
  tab: MobileTab;
  sessions: Session[];
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
  selfName: string;
  selfWxid: string;
  selfAvatar: string;
  selfProfile?: ContactProfile;
  contactsLoading: boolean;
  dark: boolean;
  onSwitchTab: (tab: MobileTab) => void;
  onSelectChat: (wxid: string, fallback?: Partial<Session>) => void;
  onSelectContact: (entry: DirectoryEntry) => void;
  onHydrateContacts: () => void;
  onOpenSelfDetail: () => void;
  onBackToAccounts: () => void;
}) {
  return (
    <div className={`h-dvh w-screen overflow-hidden flex flex-col ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#ededed] text-[#111]"}`}>
      {tab === "chats" && <MobileChatsView sessions={sessions} onSelectChat={onSelectChat} onBackToAccounts={onBackToAccounts} dark={dark} />}
      {tab === "contacts" && (
        <MobileContactsView
          friends={friends}
          groups={groups}
          loading={contactsLoading}
          dark={dark}
          onHydrate={onHydrateContacts}
          onSelect={onSelectContact}
        />
      )}
      {tab === "me" && (
        <MobileMeView
          selfName={selfName}
          selfWxid={selfWxid}
          selfAvatar={selfAvatar}
          profile={selfProfile}
          dark={dark}
          onOpenSelfDetail={onOpenSelfDetail}
        />
      )}
      <MobileTabBar active={tab} onChange={onSwitchTab} dark={dark} />
    </div>
  );
}

function MobileSearchBar({ placeholder = "Search", dark = false }: { placeholder?: string; dark?: boolean }) {
  return (
    <div className="h-[44px] px-[12px] flex items-center">
      <div className={`w-full h-[36px] rounded-[7px] flex items-center justify-center gap-[7px] ${dark ? "bg-[#242424] text-[#777]" : "bg-white text-[#b7b7b7]"}`}>
        <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.2-5.2M10.8 18a7.2 7.2 0 1 1 0-14.4 7.2 7.2 0 0 1 0 14.4Z" />
        </svg>
        <span className="text-[16px]">{placeholder}</span>
      </div>
    </div>
  );
}

function MobileChatsView({
  sessions,
  onSelectChat,
  onBackToAccounts,
  dark,
}: {
  sessions: Session[];
  onSelectChat: (wxid: string) => void;
  onBackToAccounts: () => void;
  dark: boolean;
}) {
  const touchStartRef = useRef<{ x: number; y: number } | null>(null);

  const handleTouchStart = (event: TouchEvent<HTMLDivElement>) => {
    const touch = event.touches[0];
    if (!touch) return;
    touchStartRef.current = { x: touch.clientX, y: touch.clientY };
  };

  const handleTouchEnd = (event: TouchEvent<HTMLDivElement>) => {
    const start = touchStartRef.current;
    touchStartRef.current = null;
    const touch = event.changedTouches[0];
    if (!start || !touch) return;
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    if (dx > 90 && Math.abs(dx) > Math.abs(dy) * 1.35) {
      onBackToAccounts();
    }
  };

  return (
    <div
      className="flex-1 min-h-0 overflow-y-auto pt-[calc(env(safe-area-inset-top)+8px)] pb-[10px]"
      onTouchStart={handleTouchStart}
      onTouchEnd={handleTouchEnd}
    >
      <MobileSearchBar dark={dark} />
      <div className={dark ? "bg-[#111111]" : "bg-white"}>
        {sessions.length === 0 && <div className={`text-center text-[14px] py-[48px] ${dark ? "text-[#666]" : "text-[#999]"}`}>暂无会话</div>}
        {sessions.map((session) => (
          <MobileSessionRow key={session.wxid} session={session} onClick={() => onSelectChat(session.wxid)} dark={dark} />
        ))}
      </div>
    </div>
  );
}

function MobileSessionRow({ session, onClick, dark }: { session: Session; onClick: () => void; dark: boolean }) {
  return (
    <button type="button" onClick={onClick} className={`w-full h-[74px] pl-[14px] pr-[12px] flex items-center gap-[12px] text-left ${dark ? "active:bg-[#242424]" : "active:bg-[#f4f4f4]"}`}>
      <MobileAvatar name={session.nickname || session.wxid} avatar={session.avatar} group={session.is_group} size={52} pinned={session.pinned} />
      <div className={`min-w-0 flex-1 h-full border-b flex flex-col justify-center ${dark ? "border-[#242424]" : "border-[#ededed]"}`}>
        <div className="flex items-baseline gap-[8px]">
          <div className="text-[17px] leading-[23px] truncate flex-1">{session.nickname || session.wxid}</div>
          <div className={`text-[12px] shrink-0 ${dark ? "text-[#666]" : "text-[#b8b8b8]"}`}>{session.lastTime || ""}</div>
        </div>
        <div className="mt-[3px] flex items-center gap-[6px]">
          <div className={`text-[14px] truncate flex-1 ${dark ? "text-[#777]" : "text-[#aaa]"}`}>{session.lastMsg || ""}</div>
          {session.muted && <span className={`text-[12px] ${dark ? "text-[#666]" : "text-[#b8b8b8]"}`}>静音</span>}
          {!session.muted && session.unread && session.unread > 0 ? (
            <span className="min-w-[18px] h-[18px] rounded-full bg-[#fa5151] text-white text-[11px] flex items-center justify-center px-[5px]">
              {session.unread > 99 ? "99+" : session.unread}
            </span>
          ) : null}
        </div>
      </div>
    </button>
  );
}

function MobileContactsView({
  friends,
  groups,
  loading,
  dark,
  onHydrate,
  onSelect,
}: {
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
  loading: boolean;
  dark: boolean;
  onHydrate: () => void;
  onSelect: (entry: DirectoryEntry) => void;
}) {
  const [query, setQuery] = useState("");
  useEffect(() => {
    onHydrate();
  }, [onHydrate]);
  const q = query.trim().toLowerCase();
  const filteredFriends = friends.filter((entry) => !q || entry.name.toLowerCase().includes(q) || entry.wxid.toLowerCase().includes(q));
  const filteredGroups = groups.filter((entry) => !q || entry.name.toLowerCase().includes(q) || entry.wxid.toLowerCase().includes(q));
  const friendSections = groupDirectoryEntries(filteredFriends);
  const indexLetters = ["⌕", ...friendSections.map((section) => section.title)];

  return (
    <div className="relative flex-1 min-h-0 overflow-y-auto pb-[10px]">
      <MobileTopBar dark={dark} title="Contacts" rightLabel="＋" />
      <div className="h-[44px] px-[12px] flex items-center">
        <div className={`w-full h-[36px] rounded-[7px] flex items-center gap-[7px] px-[12px] ${dark ? "bg-[#242424] text-[#777]" : "bg-white text-[#b7b7b7]"}`}>
          <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.2-5.2M10.8 18a7.2 7.2 0 1 1 0-14.4 7.2 7.2 0 0 1 0 14.4Z" />
          </svg>
          <input value={query} onChange={(e) => setQuery(e.target.value)} className={`bg-transparent outline-none flex-1 text-[16px] ${dark ? "text-[#e8e8e8] placeholder-[#777]" : "text-[#111] placeholder-[#b7b7b7]"}`} placeholder="Search" />
        </div>
      </div>
      {loading && <div className={`px-[22px] py-[6px] text-[12px] ${dark ? "text-[#777]" : "text-[#999]"}`}>正在补全联系人资料...</div>}
      <div className={dark ? "bg-[#111111]" : "bg-white"}>
        <MobileContactStaticRow dark={dark} color="#ffad33" label="New Friends" icon="person+" />
        <MobileContactStaticRow dark={dark} color="#ffad33" label="Chats Only Friends" icon="person" />
        <MobileContactStaticRow dark={dark} color="#07c160" label="Group Chats" icon="group" />
        <MobileContactStaticRow dark={dark} color="#1e9bf0" label="Tags" icon="tag" />
        <MobileContactStaticRow dark={dark} color="#1688f0" label="Official Accounts" icon="leaf" />
        <MobileContactStaticRow dark={dark} color="#21a8f4" label="Service Accounts" icon="diamond" />
      </div>
      {filteredGroups.length > 0 && <MobileContactSection dark={dark} title="群聊" entries={filteredGroups} onSelect={onSelect} />}
      {friendSections.map((section) => (
        <MobileContactSection key={section.title} dark={dark} title={section.title} entries={section.entries} onSelect={onSelect} />
      ))}
      {friendSections.length > 0 && (
        <div className={`fixed right-[5px] top-[34%] z-10 flex flex-col items-center gap-[2px] text-[11px] leading-[13px] ${dark ? "text-[#888]" : "text-[#444]"}`}>
          {indexLetters.map((letter) => (
            <span key={letter}>{letter}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function MobileContactStaticRow({ color, label, icon, dark }: { color: string; label: string; icon: string; dark: boolean }) {
  return (
    <div className="h-[58px] pl-[22px] pr-[12px] flex items-center gap-[14px]">
      <div className="w-[38px] h-[38px] rounded-[5px] flex items-center justify-center text-white" style={{ backgroundColor: color }}>
        <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          {icon === "tag" && <path strokeLinecap="round" strokeLinejoin="round" d="m4 12 8-8h7v7l-8 8-7-7Zm12-5h.01" />}
          {icon === "leaf" && <path strokeLinecap="round" strokeLinejoin="round" d="M5 5c8 0 13 5 14 14-8-1-14-6-14-14Zm0 0c4 5 8 9 14 14" />}
          {icon === "diamond" && <path strokeLinecap="round" strokeLinejoin="round" d="m12 4 7 8-7 8-7-8 7-8Zm-7 8h14" />}
          {icon === "group" && <path strokeLinecap="round" strokeLinejoin="round" d="M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0ZM4.5 21c.8-4.2 3.3-6.3 7.5-6.3s6.7 2.1 7.5 6.3" />}
          {icon !== "tag" && icon !== "leaf" && icon !== "diamond" && icon !== "group" && <path strokeLinecap="round" strokeLinejoin="round" d="M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM5 21c.8-4 3.1-6 7-6s6.2 2 7 6M18 6v4M20 8h-4" />}
        </svg>
      </div>
      <div className={`flex-1 h-full border-b flex items-center text-[17px] ${dark ? "border-[#242424]" : "border-[#ededed]"}`}>{label}</div>
    </div>
  );
}

function MobileContactSection({ title, entries, onSelect, dark }: { title: string; entries: DirectoryEntry[]; onSelect: (entry: DirectoryEntry) => void; dark: boolean }) {
  return (
    <div>
      <div className={`h-[34px] px-[22px] flex items-center text-[14px] ${dark ? "text-[#888]" : "text-[#777]"}`}>{title}</div>
      <div className={dark ? "bg-[#111111]" : "bg-white"}>
        {entries.map((entry) => (
          <button key={`${entry.source}_${entry.wxid}`} type="button" onClick={() => onSelect(entry)} className={`w-full h-[62px] pl-[22px] pr-[12px] flex items-center gap-[12px] text-left ${dark ? "active:bg-[#242424]" : "active:bg-[#f4f4f4]"}`}>
            <MobileAvatar name={entry.name || entry.wxid} avatar={entry.avatar} group={entry.is_group} size={42} />
            <div className={`flex-1 min-w-0 h-full border-b flex items-center ${dark ? "border-[#242424]" : "border-[#ededed]"}`}>
              <div className="text-[17px] truncate">{entry.name || entry.wxid}</div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function MobileMeView({
  selfName,
  selfWxid,
  selfAvatar,
  profile,
  dark,
  onOpenSelfDetail,
}: {
  selfName: string;
  selfWxid: string;
  selfAvatar: string;
  profile?: ContactProfile;
  dark: boolean;
  onOpenSelfDetail: () => void;
}) {
  const raw = profile?.profile || {};
  const alias = raw.Alias || raw.alias || selfWxid;
  return (
    <div className="flex-1 min-h-0 overflow-y-auto pb-[10px]">
      <div className={`h-[76px] pt-[env(safe-area-inset-top)] ${dark ? "bg-[#111111]" : "bg-white"}`} />
      <button type="button" onClick={onOpenSelfDetail} className={`w-full px-[34px] py-[26px] flex items-center gap-[22px] text-left ${dark ? "bg-[#111111] active:bg-[#242424]" : "bg-white active:bg-[#f7f7f7]"}`}>
        <MobileAvatar name={selfName} avatar={selfAvatar} size={78} />
        <div className="min-w-0 flex-1">
          <div className="text-[24px] font-semibold truncate">{selfName}</div>
          <div className={`text-[16px] mt-[7px] truncate ${dark ? "text-[#888]" : "text-[#777]"}`}>Weixin ID: {alias || selfWxid}</div>
        </div>
        <div className={`text-[24px] ${dark ? "text-[#666]" : "text-[#b8b8b8]"}`}>›</div>
      </button>
      <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
      <MobileMeRow dark={dark} label="Pay and Services" color="#07c160" />
      <MobileMeRow dark={dark} label="Favorites" color="#ff7043" />
      <MobileMeRow dark={dark} label="Moments" color="#1e9bf0" />
      <MobileMeRow dark={dark} label="Works" color="#21a8f4" />
      <MobileMeRow dark={dark} label="Stores and Cards" color="#ff6b6b" />
      <MobileMeRow dark={dark} label="Sticker Gallery" color="#ffc300" />
      <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
      <MobileMeRow dark={dark} label="Settings" color="#1e9bf0" />
    </div>
  );
}

function MobileMeRow({ label, color, dark }: { label: string; color: string; dark: boolean }) {
  return (
    <div className={`h-[56px] pl-[28px] pr-[18px] flex items-center gap-[18px] ${dark ? "bg-[#111111]" : "bg-white"}`}>
      <div className="w-[22px] h-[22px] rounded-[5px] border-2" style={{ borderColor: color }} />
      <div className={`flex-1 h-full border-b flex items-center text-[17px] ${dark ? "border-[#242424]" : "border-[#ededed]"}`}>{label}</div>
      <div className={`text-[24px] ${dark ? "text-[#666]" : "text-[#b8b8b8]"}`}>›</div>
    </div>
  );
}

function MobileProfileDetailPage({
  profile,
  fallbackName,
  fallbackAvatar,
  loading,
  onBack,
  onAvatarClick,
  dark = false,
}: {
  profile?: ContactProfile;
  fallbackName: string;
  fallbackAvatar: string;
  loading: boolean;
  onBack: () => void;
  onAvatarClick: () => void;
  dark?: boolean;
}) {
  const raw = profile?.profile || {};
  const name = profileDisplayName(profile, fallbackName);
  const avatar = profileAvatar(profile, fallbackAvatar);
  const alias = raw.Alias || raw.alias || raw.account || profile?.wxid || "";
  const gender = String(raw.Sex || raw.sex || "") === "1" ? "Male" : String(raw.Sex || raw.sex || "") === "2" ? "Female" : "";
  const phone = raw.Mobile || raw.mobile || raw.Phone || raw.phone || "";
  const signature = raw.Signature || raw.signature || raw.Description || "";
  const area = profileArea(raw);
  return (
    <div className={`h-dvh w-screen overflow-hidden flex flex-col ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#ededed] text-[#111]"}`}>
      <MobileTopBar dark={dark} title="Profile" leftLabel="‹" onLeft={onBack} />
      <div className="flex-1 overflow-y-auto">
        <MobileProfileRow dark={dark} label="Profile Photo" value="" onClick={onAvatarClick} image={avatar} />
        <MobileProfileRow dark={dark} label="Name" value={name} />
        {gender && <MobileProfileRow dark={dark} label="Gender" value={gender} />}
        <MobileProfileRow dark={dark} label="Region" value={area || ""} />
        {phone && <MobileProfileRow dark={dark} label="Phone" value={phone} />}
        <MobileProfileRow dark={dark} label="ID" value={alias} />
        <MobileProfileRow dark={dark} label="My QR Code" value="▦" />
        {profile?.wxid && <MobileProfileRow dark={dark} label="Tickle" value={profile.wxid} />}
        <MobileProfileRow dark={dark} label="What's Up" value={signature} />
        <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
        <MobileProfileRow dark={dark} label="Incoming Call Ringtones" value="" />
        <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
        <MobileProfileRow dark={dark} label="My Address" value="" />
        <MobileProfileRow dark={dark} label="My Fapiao Titles" value="" />
        <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
        <MobileProfileRow dark={dark} label="WeBeans" value="" />
        {loading && <div className={`px-[22px] py-[14px] text-[13px] ${dark ? "text-[#777]" : "text-[#999]"}`}>正在加载资料...</div>}
      </div>
    </div>
  );
}

function MobileProfileRow({ label, value, image, onClick, dark }: { label: string; value?: string; image?: string; onClick?: () => void; dark: boolean }) {
  return (
    <button type="button" onClick={onClick} className={`w-full min-h-[58px] pl-[22px] pr-[16px] flex items-center text-left ${dark ? "bg-[#111111] active:bg-[#242424]" : "bg-white active:bg-[#f7f7f7]"}`}>
      <div className="text-[17px] flex-1">{label}</div>
      {image ? <img src={image} alt="" className="w-[38px] h-[38px] rounded-[4px] object-cover" /> : <div className={`max-w-[58%] text-[16px] truncate ${dark ? "text-[#888]" : "text-[#888]"}`}>{value || ""}</div>}
      <div className={`text-[24px] ml-[8px] ${dark ? "text-[#666]" : "text-[#b8b8b8]"}`}>›</div>
    </button>
  );
}

function MobileTabBar({ active, onChange, dark }: { active: MobileTab; onChange: (tab: MobileTab) => void; dark: boolean }) {
  return (
    <div className={`shrink-0 h-[64px] pb-[env(safe-area-inset-bottom)] border-t grid grid-cols-3 ${dark ? "bg-[#111111]/95 border-[#242424]" : "bg-white/95 border-[#dedede]"}`}>
      <MobileTabButton active={active === "chats"} label="Chats" onClick={() => onChange("chats")} icon="chat" dark={dark} />
      <MobileTabButton active={active === "contacts"} label="Contacts" onClick={() => onChange("contacts")} icon="contacts" dark={dark} />
      <MobileTabButton active={active === "me"} label="Me" onClick={() => onChange("me")} icon="me" dark={dark} />
    </div>
  );
}

function MobileTabButton({ active, label, icon, onClick, dark }: { active: boolean; label: string; icon: string; onClick: () => void; dark: boolean }) {
  return (
    <button type="button" onClick={onClick} className={`flex flex-col items-center justify-center gap-[2px] ${active ? "text-[#07c160]" : (dark ? "text-[#cfcfcf]" : "text-[#222]")}`}>
      <svg className="w-[25px] h-[25px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
        {icon === "chat" && <path strokeLinecap="round" strokeLinejoin="round" d="M4 6.5A3.5 3.5 0 0 1 7.5 3h9A3.5 3.5 0 0 1 20 6.5v5A3.5 3.5 0 0 1 16.5 15H11l-5 4v-4.35A3.5 3.5 0 0 1 4 11.5v-5Z" />}
        {icon === "contacts" && <path strokeLinecap="round" strokeLinejoin="round" d="M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0ZM4.5 21c.8-4.2 3.3-6.3 7.5-6.3s6.7 2.1 7.5 6.3M18 6v4M20 8h-4" />}
        {icon === "me" && <path strokeLinecap="round" strokeLinejoin="round" d="M16 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM5 21c.8-4 3.1-6 7-6s6.2 2 7 6" />}
      </svg>
      <span className="text-[11px] leading-[13px]">{label}</span>
    </button>
  );
}

function MobileAvatar({ name, avatar, group, size = 42, pinned = false }: { name: string; avatar?: string; group?: boolean; size?: number; pinned?: boolean }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      {avatar && !failed ? (
        <img src={avatar} alt="" className="w-full h-full rounded-[6px] object-cover" onError={() => setFailed(true)} loading="lazy" />
      ) : (
        <div className={`w-full h-full rounded-[6px] text-white flex items-center justify-center ${group ? "bg-[#576b95]" : "bg-[#07c160]"}`} style={{ fontSize: Math.max(15, size * 0.38) }}>
          {(name || "?")[0]}
        </div>
      )}
      {pinned ? (
        <span className="absolute -left-[3px] -top-[3px] w-[16px] h-[16px] rounded-full bg-[#07c160] text-white shadow-sm flex items-center justify-center">
          <svg className="w-[9px] h-[9px]" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M14.8 2.8 21.2 9.2 18 10.4 14.3 14.1 14.9 19.3 13.4 20.8 9 16.4 4.2 21.2 2.8 19.8 7.6 15 3.2 10.6 4.7 9.1 9.9 9.7 13.6 6 14.8 2.8Z" />
          </svg>
        </span>
      ) : null}
    </div>
  );
}

function MultiAccountBroadcastPanel({ accounts, theme }: { accounts: WeChatAccount[]; theme: PortalTheme }) {
  const [targetTypes, setTargetTypes] = useState<Set<string>>(new Set(["friends"]));
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

  const agentIds = Array.from(selectedAgents).filter(Boolean);
  const selectedTargetTypes = Array.from(targetTypes);
  const dark = theme === "dark";

  const toggleAgent = (id: string) => {
    setSelectedAgents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleTargetType = (type: string) => {
    setTargetTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const sendText = async () => {
    if (!message.trim() || selectedTargetTypes.length === 0 || agentIds.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastText(agentIds, selectedTargetTypes, message.trim());
      setResultText(`文本完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  const sendImage = async () => {
    if (!image || selectedTargetTypes.length === 0 || agentIds.length === 0 || sending) return;
    setSending(true);
    setResultText("");
    try {
      const res = await multiAccountBroadcastImageUpload(agentIds, selectedTargetTypes, image);
      setResultText(`图片完成：成功 ${res?.sent || 0}，失败 ${res?.failed || 0}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={`h-full overflow-y-auto p-[28px] ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#f4f4f4] text-[#111]"}`}>
      <div className="max-w-[760px]">
        <div className="text-[22px] font-medium">多号群发</div>
        <div className="mt-[18px] grid grid-cols-1 gap-[14px]">
          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">发送账号</div>
            <div className="flex flex-wrap gap-[8px]">
              {accounts.map((account) => (
                <label key={account.id} className={`h-[34px] px-[10px] rounded-[4px] border flex items-center gap-[7px] cursor-pointer ${
                  dark ? "bg-[#1d1d1d] border-[#303030]" : "bg-white border-[#d8d8d8]"
                }`}>
                  <input
                    type="checkbox"
                    checked={selectedAgents.has(account.id)}
                    onChange={() => toggleAgent(account.id)}
                    className="accent-[#07c160]"
                  />
                  <span className="text-[13px]">
                    {(account.nickname && account.nickname !== account.id ? account.nickname : "") || account.wxid || account.id}
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">目标类型</div>
            <div className="grid grid-cols-2 gap-[10px] max-w-[420px]">
              <TargetTypeButton
                active={targetTypes.has("friends")}
                dark={dark}
                title="所有个人"
                subtitle="每个账号的好友"
                onClick={() => toggleTargetType("friends")}
              />
              <TargetTypeButton
                active={targetTypes.has("groups")}
                dark={dark}
                title="所有群"
                subtitle="每个账号的群聊"
                onClick={() => toggleTargetType("groups")}
              />
            </div>
          </div>

          <div>
            <div className="text-[13px] text-[#888] mb-[8px]">文本消息</div>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              className={`w-full h-[100px] resize-none rounded-[4px] border outline-none px-[10px] py-[8px] text-[14px] focus:border-[#07c160] ${
                dark ? "bg-[#1d1d1d] border-[#303030] text-[#eee]" : "bg-white border-[#d8d8d8] text-[#111]"
              }`}
              placeholder="输入文本"
            />
            <button
              type="button"
            disabled={sending || !message.trim() || selectedTargetTypes.length === 0 || agentIds.length === 0}
            onClick={sendText}
              className={`mt-[10px] h-[36px] px-[18px] rounded-[4px] bg-[#07c160] text-white active:opacity-85 ${dark ? "disabled:bg-[#315541]" : "disabled:bg-[#b9d9c7]"}`}
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
              className={`block text-[13px] ${dark ? "text-[#aaa]" : "text-[#555]"}`}
            />
            {preview && <img src={preview} alt="" className={`mt-[10px] max-w-[180px] max-h-[140px] rounded-[4px] object-contain ${dark ? "bg-[#1d1d1d]" : "bg-white border border-[#e0e0e0]"}`} />}
            <button
              type="button"
              disabled={sending || !image || selectedTargetTypes.length === 0 || agentIds.length === 0}
              onClick={sendImage}
              className={`mt-[10px] h-[36px] px-[18px] rounded-[4px] bg-[#07c160] text-white active:opacity-85 ${dark ? "disabled:bg-[#315541]" : "disabled:bg-[#b9d9c7]"}`}
            >
              {sending ? "发送中" : "群发图片"}
            </button>
          </div>

          <div className="text-[13px] text-[#888]">
            已选账号 {agentIds.length} 个，目标类型 {selectedTargetTypes.length} 个。{resultText}
          </div>
        </div>
      </div>
    </div>
  );
}

function TargetTypeButton({
  active,
  dark = true,
  title,
  subtitle,
  onClick,
}: {
  active: boolean;
  dark?: boolean;
  title: string;
  subtitle: string;
  onClick: () => void;
}) {
  const inactiveClass = dark ? "border-[#303030] bg-[#1d1d1d]" : "border-[#d8d8d8] bg-white";
  const activeClass = dark ? "border-[#07c160] bg-[#123d27]" : "border-[#07c160] bg-[#e9f8ef]";
  const titleClass = active
    ? (dark ? "text-[#f2f2f2]" : "text-[#0d3f24]")
    : (dark ? "text-[#f2f2f2]" : "text-[#111]");
  const subtitleClass = active
    ? (dark ? "text-[#9ab5a4]" : "text-[#4f7f63]")
    : (dark ? "text-[#888]" : "text-[#777]");
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-h-[70px] rounded-[6px] border px-[12px] py-[10px] text-left active:opacity-85 ${
        active ? activeClass : inactiveClass
      }`}
    >
      <div className="flex items-center gap-[8px]">
        <span className={`w-[18px] h-[18px] rounded-[4px] border flex items-center justify-center ${
          active ? "bg-[#07c160] border-[#07c160]" : (dark ? "border-[#555]" : "border-[#aaa]")
        }`}>
          {active && (
            <svg className="w-[13px] h-[13px] text-white" fill="none" stroke="currentColor" strokeWidth={2.4} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
            </svg>
          )}
        </span>
        <span className={`text-[16px] ${titleClass}`}>{title}</span>
      </div>
      <div className={`mt-[7px] text-[12px] ${subtitleClass}`}>{subtitle}</div>
    </button>
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
  dark,
  onHydrate,
  onSelect,
}: {
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
  loading: boolean;
  dark: boolean;
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
  const friendSections = groupDirectoryEntries(visibleFriends);

  return (
    <div className={`h-full flex flex-col ${dark ? "bg-[#191919] text-[#e8e8e8]" : "bg-[#e9e8e8] text-[#111]"}`}>
      <div className="h-[92px] px-[18px] flex items-center gap-[12px] shrink-0">
        <div className={`flex-1 h-[38px] rounded-[4px] flex items-center px-[10px] ${dark ? "bg-[#262626]" : "bg-[#dcdcdc]"}`}>
          <svg className={`w-[18px] h-[18px] shrink-0 ${dark ? "text-[#666]" : "text-[#777]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className={`ml-[8px] bg-transparent outline-none text-[15px] w-full ${dark ? "text-[#ddd] placeholder-[#5c5c5c]" : "text-[#111] placeholder-[#888]"}`}
            placeholder="搜索"
          />
        </div>
        <button
          type="button"
          className={`w-[38px] h-[38px] rounded-[4px] flex items-center justify-center ${dark ? "bg-[#262626] text-[#999] active:bg-[#303030]" : "bg-[#dcdcdc] text-[#555] active:bg-[#d0d0d0]"}`}
          title="刷新详情"
          onClick={onHydrate}
        >
          <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path d="M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0ZM4.5 21c.8-4.2 3.3-6.3 7.5-6.3s6.7 2.1 7.5 6.3M18 5v4M20 7h-4" />
          </svg>
        </button>
      </div>

      {loading && (
        <div className={`px-[18px] pb-[8px] text-[12px] shrink-0 ${dark ? "text-[#777]" : "text-[#888]"}`}>
          正在从本地缓存补全联系人资料...
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <ContactSection dark={dark} title="群聊" entries={visibleGroups} onSelect={onSelect} />
        {friendSections.map((section) => (
          <ContactSection key={section.title} dark={dark} title={section.title} entries={section.entries} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}

function ContactSection({
  title,
  entries,
  onSelect,
  dark,
}: {
  title: string;
  entries: DirectoryEntry[];
  onSelect: (entry: DirectoryEntry) => void;
  dark: boolean;
}) {
  if (entries.length === 0) return null;
  return (
    <div>
      <div className={`px-[18px] py-[10px] text-[14px] ${dark ? "text-[#777]" : "text-[#8a8a8a]"}`}>{title}</div>
      {entries.map((entry) => (
        <button
          key={`${entry.source}_${entry.wxid}`}
          type="button"
          onClick={() => onSelect(entry)}
          className={`w-full h-[64px] px-[14px] flex items-center gap-[12px] text-left ${dark ? "hover:bg-[#242424] active:bg-[#2a2a2a]" : "hover:bg-[#dedede] active:bg-[#d3d3d3]"}`}
        >
          <EntryAvatar entry={entry} />
          <div className="min-w-0 flex-1">
            <div className={`text-[16px] truncate ${dark ? "text-[#e8e8e8]" : "text-[#111]"}`}>{entry.name || entry.wxid}</div>
            {entry.wxid && entry.wxid !== entry.name && (
              <div className={`text-[12px] truncate mt-[3px] ${dark ? "text-[#666]" : "text-[#999]"}`}>{entry.wxid}</div>
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
  dark,
}: {
  friends: DirectoryEntry[];
  groups: DirectoryEntry[];
  dark: boolean;
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
    <div className={`h-full flex flex-col ${dark ? "bg-[#191919] text-[#e8e8e8]" : "bg-[#e9e8e8] text-[#111]"}`} onPaste={handlePaste}>
      <div className="h-[92px] px-[18px] flex items-center">
        <div className={`flex-1 h-[38px] rounded-[4px] flex items-center px-[10px] ${dark ? "bg-[#262626]" : "bg-[#dcdcdc]"}`}>
          <svg className={`w-[18px] h-[18px] shrink-0 ${dark ? "text-[#666]" : "text-[#777]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className={`ml-[8px] bg-transparent outline-none text-[15px] w-full ${dark ? "text-[#ddd] placeholder-[#5c5c5c]" : "text-[#111] placeholder-[#888]"}`}
            placeholder="搜索群发对象"
          />
        </div>
      </div>

      <div className="px-[18px] flex flex-wrap gap-[8px] shrink-0">
        <BroadcastSelectButton dark={dark} label={`全选好友 ${friends.length}`} onClick={() => selectEntries(friends)} />
        <BroadcastSelectButton dark={dark} label={`全选群 ${groups.length}`} onClick={() => selectEntries(groups)} />
        <BroadcastSelectButton dark={dark} label="清空" onClick={() => setSelected(new Set())} />
      </div>

      <div className="px-[18px] py-[12px] shrink-0">
        <textarea
          ref={messageInputRef}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          className={`w-full h-[94px] resize-none rounded-[4px] border outline-none px-[10px] py-[8px] text-[15px] ${dark ? "bg-[#1d1d1d] border-[#303030] text-[#eee] placeholder-[#666]" : "bg-white border-[#d8d8d8] text-[#111]"}`}
          placeholder="输入要群发的消息"
        />
        {broadcastImages.length > 0 && (
          <div className="mt-[8px] flex flex-wrap gap-[8px]">
            {broadcastImages.map((image) => (
              <div
                key={image.id}
                className={`inline-flex items-center gap-[8px] rounded-[4px] border p-[6px] ${dark ? "border-[#303030] bg-[#1d1d1d]" : "border-[#d8d8d8] bg-white"}`}
              >
                <img src={image.preview} alt="" className="w-[54px] h-[54px] rounded-[3px] object-cover" />
                <div className={`min-w-0 max-w-[160px] text-[13px] ${dark ? "text-[#aaa]" : "text-[#555]"}`}>
                  <div className="truncate">{image.label}</div>
                  <div className={`mt-[3px] truncate ${dark ? "text-[#666]" : "text-[#999]"}`}>{image.token}</div>
                </div>
                <button
                  type="button"
                  onClick={() => removeBroadcastImage(image)}
                  className={`w-[26px] h-[26px] rounded-[4px] ${dark ? "text-[#888] active:bg-[#2a2a2a]" : "text-[#777] active:bg-[#f2f2f2]"}`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className={`mt-[8px] flex items-center justify-between text-[13px] ${dark ? "text-[#888]" : "text-[#777]"}`}>
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

      <div className={`flex-1 overflow-y-auto border-t ${dark ? "border-[#2a2a2a]" : "border-[#d8d8d8]"}`}>
        {visible.map((entry) => (
          <label
            key={`${entry.source}_${entry.wxid}`}
            className={`h-[60px] px-[14px] flex items-center gap-[10px] cursor-pointer ${dark ? "hover:bg-[#242424]" : "hover:bg-[#dedede]"}`}
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
              <div className={`text-[12px] truncate ${dark ? "text-[#666]" : "text-[#999]"}`}>{entry.is_group ? "群聊" : "好友"} · {entry.wxid}</div>
            </div>
          </label>
        ))}
      </div>
    </div>
  );
}

function BroadcastSelectButton({ label, onClick, dark }: { label: string; onClick: () => void; dark: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-[30px] px-[10px] rounded-[4px] border text-[13px] ${dark ? "bg-[#1d1d1d] border-[#303030] text-[#ddd] active:bg-[#2a2a2a]" : "bg-white border-[#d4d4d4] text-[#333] active:bg-[#f2f2f2]"}`}
    >
      {label}
    </button>
  );
}

interface GroupMemberBrief {
  wxid: string;
  name: string;
  avatar: string;
}

function DirectoryProfilePane({
  entry,
  profile,
  fallbackAvatar,
  loading,
  dark,
  onMessage,
}: {
  entry: DirectoryEntry;
  profile?: ContactProfile;
  fallbackAvatar: string;
  loading: boolean;
  dark: boolean;
  onMessage: () => void;
}) {
  const raw = profile?.profile || {};
  const name = profileDisplayName(profile, entry.name || entry.wxid);
  const avatar = profileAvatar(profile, fallbackAvatar);
  const alias = profileField(raw, ["Alias", "alias", "WXAccount", "account"]);
  const area = profileArea(raw);
  const remark = profileField(raw, ["Remark", "remark", "markname"]);
  const phone = profileField(raw, ["Mobile", "mobile", "Phone", "phone", "tel", "Tel"]);
  const labelText = profileField(raw, ["LabelText", "LabelName", "LabelNames", "labelText", "labelname"]);
  const sign = profileField(raw, ["SignInfo", "Signature", "signature", "Description", "sign"]);
  const sourceText = sourceLabel(raw.Source ?? raw.source);
  const isOpenIM = entry.wxid.endsWith("@openim") || Boolean(raw.OpenIM || raw.openim_detail);

  return (
    <div className={`h-full overflow-y-auto ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#f5f5f5] text-[#111]"}`}>
      <div className="max-w-[620px] mx-auto px-[48px] pt-[74px] pb-[48px]">
        {entry.is_group ? (
          <GroupDirectoryProfile entry={entry} dark={dark} loading={loading} onMessage={onMessage} />
        ) : (
          <div className={`w-full ${dark ? "text-[#e8e8e8]" : "text-[#111]"}`}>
            <div className="flex items-start gap-[22px]">
              <MobileAvatar name={name} avatar={avatar} size={76} />
              <div className="min-w-0 flex-1 pt-[2px]">
                <div className="flex items-center gap-[8px]">
                  <div className="text-[24px] leading-[30px] font-medium truncate">{name}</div>
                  {!isOpenIM && (
                    <svg className="w-[18px] h-[18px] text-[#1e9bf0] shrink-0" viewBox="0 0 24 24" fill="currentColor">
                      <circle cx="12" cy="7" r="4" />
                      <path d="M4.8 21c.8-4.2 3.2-6.3 7.2-6.3s6.4 2.1 7.2 6.3H4.8Z" />
                    </svg>
                  )}
                </div>
                <div className={`mt-[5px] text-[16px] leading-[24px] truncate ${dark ? "text-[#888]" : "text-[#999]"}`}>
                  微信号：{alias || entry.wxid}
                </div>
                {area && <div className={`text-[16px] leading-[24px] truncate ${dark ? "text-[#888]" : "text-[#999]"}`}>地区：{area}</div>}
                {loading && <div className={`mt-[10px] text-[13px] ${dark ? "text-[#777]" : "text-[#999]"}`}>正在加载资料...</div>}
              </div>
              <div className={`text-[24px] ${dark ? "text-[#777]" : "text-[#999]"}`}>...</div>
            </div>

            <div className={`h-px my-[28px] ${dark ? "bg-[#2a2a2a]" : "bg-[#e3e3e3]"}`} />
            <ProfileInfoRows
              dark={dark}
              rows={[
                ["备注", remark || (profile?.name && profile.name !== name ? profile.name : "")],
                ["电话", phone],
                ["标签", labelText],
              ]}
            />
            <div className={`h-px my-[24px] ${dark ? "bg-[#2a2a2a]" : "bg-[#e3e3e3]"}`} />
            <ProfileInfoRows
              dark={dark}
              rows={[
                ["个性签名", sign],
                ["来源", sourceText],
              ]}
            />
            <div className={`h-px my-[26px] ${dark ? "bg-[#2a2a2a]" : "bg-[#e3e3e3]"}`} />
            <div className="flex justify-center">
              <button
                type="button"
                onClick={onMessage}
                className="w-[166px] h-[48px] rounded-[2px] bg-[#07c160] text-white text-[18px] active:opacity-85"
              >
                发消息
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ProfileInfoRows({ rows, dark }: { rows: Array<[string, string | undefined]>; dark: boolean }) {
  const visible = rows.filter(([, value]) => String(value || "").trim());
  if (visible.length === 0) return null;
  return (
    <div className="space-y-[10px]">
      {visible.map(([label, value]) => (
        <div key={label} className="grid grid-cols-[98px_1fr] gap-[12px] text-[16px] leading-[24px]">
          <div className={dark ? "text-[#888]" : "text-[#999]"}>{label}</div>
          <div className="min-w-0 break-words">{value}</div>
        </div>
      ))}
    </div>
  );
}

function GroupDirectoryProfile({
  entry,
  dark,
  loading,
  onMessage,
}: {
  entry: DirectoryEntry;
  dark: boolean;
  loading: boolean;
  onMessage: () => void;
}) {
  const [members, setMembers] = useState<GroupMemberBrief[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setMembersLoading(true);
    getGroupMemberDetails(entry.wxid)
      .then((data: any) => {
        if (!alive) return;
        const rawMembers = data?.members && typeof data.members === "object" ? data.members : {};
        const next = Object.entries<any>(rawMembers).map(([wxid, info]) => ({
          wxid,
          name: String(info?.name || wxid),
          avatar: String(info?.avatar || ""),
        }));
        setMembers(next);
      })
      .catch((err: Error) => console.error("[GROUP_PROFILE]", err))
      .finally(() => {
        if (alive) setMembersLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [entry.wxid]);

  const shown = members.slice(0, 16);
  const memberCount = members.length || Number((entry.name.match(/（(\d+)）|\((\d+)\)/)?.[1] || entry.name.match(/（(\d+)）|\((\d+)\)/)?.[2]) || 0);

  return (
    <div className="min-h-full flex flex-col">
      <div className="text-[24px] font-medium truncate">{entry.name || entry.wxid}{memberCount ? ` (${memberCount})` : ""}</div>
      <div className={`mt-[24px] rounded-[2px] p-[24px] ${dark ? "bg-[#171717]" : "bg-white"}`}>
        {shown.length > 0 ? (
          <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 gap-x-[20px] gap-y-[22px]">
            {shown.map((member) => (
              <div key={member.wxid} className="min-w-0 flex flex-col items-center">
                <MobileAvatar name={member.name} avatar={member.avatar} size={58} />
                <div className={`mt-[8px] w-full text-center text-[13px] truncate ${dark ? "text-[#888]" : "text-[#999]"}`}>{member.name}</div>
              </div>
            ))}
          </div>
        ) : (
          <div className={`h-[96px] flex items-center justify-center text-[14px] ${dark ? "text-[#777]" : "text-[#999]"}`}>
            {membersLoading || loading ? "正在加载群成员..." : "暂无群成员资料"}
          </div>
        )}
      </div>
      <div className="flex-1 min-h-[160px]" />
      <div className="flex justify-center pb-[24px]">
        <button
          type="button"
          onClick={onMessage}
          className="w-[200px] h-[52px] rounded-[2px] bg-[#07c160] text-white text-[18px] active:opacity-85"
        >
          发消息
        </button>
      </div>
    </div>
  );
}

function MobileDirectoryProfilePage({
  entry,
  profile,
  loading,
  dark,
  onBack,
  onMessage,
}: {
  entry: DirectoryEntry;
  profile?: ContactProfile;
  loading: boolean;
  dark: boolean;
  onBack: () => void;
  onMessage: () => void;
}) {
  const raw = profile?.profile || {};
  const name = profileDisplayName(profile, entry.name || entry.wxid);
  const avatar = profileAvatar(profile, entry.avatar);
  const alias = profileField(raw, ["Alias", "alias", "WXAccount", "account"]) || entry.wxid;
  const area = profileArea(raw);
  const labelText = profileField(raw, ["LabelText", "LabelName", "LabelNames", "labelText", "labelname"]);
  const sign = profileField(raw, ["SignInfo", "Signature", "signature", "Description", "sign"]);
  const sourceText = sourceLabel(raw.Source ?? raw.source);

  return (
    <div className={`h-dvh w-screen overflow-hidden flex flex-col ${dark ? "bg-[#111111] text-[#e8e8e8]" : "bg-[#ededed] text-[#111]"}`}>
      <MobileTopBar dark={dark} title="" leftLabel="‹" rightLabel="..." onLeft={onBack} />
      <div className="flex-1 overflow-y-auto">
        <div className={`px-[22px] pt-[22px] pb-[28px] ${dark ? "bg-[#111111]" : "bg-white"}`}>
          <div className="flex items-center gap-[16px]">
            <MobileAvatar name={name} avatar={avatar} group={entry.is_group} size={68} />
            <div className="min-w-0 flex-1">
              <div className="text-[23px] font-semibold truncate">{name}</div>
              <div className={`mt-[5px] text-[16px] truncate ${dark ? "text-[#888]" : "text-[#777]"}`}>
                {entry.is_group ? entry.wxid : `Weixin ID: ${alias}`}
              </div>
              {area && <div className={`mt-[3px] text-[15px] truncate ${dark ? "text-[#888]" : "text-[#777]"}`}>{area}</div>}
            </div>
          </div>
          {loading && <div className={`mt-[12px] text-[13px] ${dark ? "text-[#777]" : "text-[#999]"}`}>正在加载资料...</div>}
        </div>
        <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
        {!entry.is_group && (
          <>
            {labelText && <MobileProfileRow dark={dark} label="Tags" value={labelText} />}
            {sign && <MobileProfileRow dark={dark} label="What's Up" value={sign} />}
            {sourceText && <MobileProfileRow dark={dark} label="Source" value={sourceText} />}
            <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
          </>
        )}
        {entry.is_group && <MobileGroupMemberStrip gid={entry.wxid} dark={dark} />}
        <button
          type="button"
          onClick={onMessage}
          className={`w-full h-[58px] flex items-center justify-center gap-[8px] border-y text-[17px] font-medium ${dark ? "bg-[#111111] border-[#242424] text-[#6f88b7] active:bg-[#242424]" : "bg-white border-[#ededed] text-[#576b95] active:bg-[#f7f7f7]"}`}
        >
          <svg className="w-[20px] h-[20px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6.5A3.5 3.5 0 0 1 7.5 3h9A3.5 3.5 0 0 1 20 6.5v5A3.5 3.5 0 0 1 16.5 15H11l-5 4v-4.35A3.5 3.5 0 0 1 4 11.5v-5Z" />
          </svg>
          Messages
        </button>
        <div className={`h-[58px] flex items-center justify-center gap-[8px] text-[16px] ${dark ? "bg-[#111111] text-[#6f88b7]" : "bg-white text-[#576b95]"}`}>
          <span>☏ Voice or Video Call</span>
        </div>
      </div>
    </div>
  );
}

function MobileGroupMemberStrip({ gid, dark }: { gid: string; dark: boolean }) {
  const [members, setMembers] = useState<GroupMemberBrief[]>([]);
  useEffect(() => {
    let alive = true;
    getGroupMemberDetails(gid)
      .then((data: any) => {
        if (!alive) return;
        const rawMembers = data?.members && typeof data.members === "object" ? data.members : {};
        setMembers(Object.entries<any>(rawMembers).slice(0, 12).map(([wxid, info]) => ({
          wxid,
          name: String(info?.name || wxid),
          avatar: String(info?.avatar || ""),
        })));
      })
      .catch((err: Error) => console.error("[MOBILE_GROUP_PROFILE]", err));
    return () => {
      alive = false;
    };
  }, [gid]);

  if (members.length === 0) return null;
  return (
    <>
      <div className={`px-[18px] py-[18px] ${dark ? "bg-[#111111]" : "bg-white"}`}>
        <div className="grid grid-cols-4 gap-y-[16px]">
          {members.map((member) => (
            <div key={member.wxid} className="min-w-0 flex flex-col items-center">
              <MobileAvatar name={member.name} avatar={member.avatar} size={48} />
              <div className={`mt-[6px] w-full px-[4px] text-center text-[12px] truncate ${dark ? "text-[#888]" : "text-[#777]"}`}>{member.name}</div>
            </div>
          ))}
        </div>
      </div>
      <div className={`h-[10px] ${dark ? "bg-[#1a1a1a]" : "bg-[#ededed]"}`} />
    </>
  );
}

function SelfProfileCard({
  profile,
  fallbackName,
  fallbackAvatar,
  loading,
  onClose,
  onAvatarClick,
  dark = true,
}: {
  profile?: ContactProfile;
  fallbackName: string;
  fallbackAvatar: string;
  loading: boolean;
  onClose: () => void;
  onAvatarClick: () => void;
  dark?: boolean;
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
        className={`absolute left-[24px] top-[86px] w-[420px] rounded-[2px] shadow-2xl border ${dark ? "bg-[#1f1f1f] text-[#e8e8e8] border-[#333]" : "bg-white text-[#111] border-[#ddd]"}`}
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
          <div className={`h-px my-[26px] ${dark ? "bg-[#333]" : "bg-[#e8e8e8]"}`} />
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

function LargeAvatarOverlay({ src, onClose, dark = true }: { src: string; onClose: () => void; dark?: boolean }) {
  return (
    <div className={`fixed inset-0 z-[9999] flex flex-col ${dark ? "bg-[#111111]" : "bg-white"}`} onClick={onClose}>
      <div className={`h-[54px] shrink-0 border-b flex items-center px-[18px] ${dark ? "border-[#242424] text-[#ddd]" : "border-[#e5e5e5] text-[#555]"}`}>
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
          <div className={dark ? "text-[#777]" : "text-[#999]"}>暂无头像</div>
        )}
      </div>
    </div>
  );
}

function EmptyChatPane({ dark = true }: { dark?: boolean }) {
  return (
    <div className={`h-full flex items-center justify-center ${dark ? "bg-[#111111]" : "bg-[#f5f5f5]"}`}>
      <div className={dark ? "text-[#2a2a2a]" : "text-[#e0e0e0]"}>
        <svg className="w-[128px] h-[96px]" viewBox="0 0 160 120" fill="currentColor">
          <path opacity=".42" d="M65 22c-25 0-45 16-45 36 0 12 7 23 19 29l-4 17 19-11c4 1 7 1 11 1 25 0 45-16 45-36S90 22 65 22Zm-17 32a7 7 0 1 1 0-14 7 7 0 0 1 0 14Zm34 0a7 7 0 1 1 0-14 7 7 0 0 1 0 14Z" />
          <path opacity=".28" d="M105 54c20 0 36 13 36 29 0 10-6 18-16 23l3 13-15-8c-3 .5-6 1-9 1-20 0-36-13-36-29s16-29 37-29Zm-12 25a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm27 0a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z" />
        </svg>
      </div>
    </div>
  );
}
