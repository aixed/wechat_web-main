// ─── Contact / Session ────────────────────────────────────────────

export interface Contact {
  wxid: string;
  nickname: string;
  avatar?: string;
  remark?: string;
  is_group: boolean;
}

export interface Session {
  wxid: string;
  nickname: string;
  avatar?: string;
  lastMsg?: string;
  lastTime?: string;
  lastTimestamp?: number;   // Unix epoch seconds – used for sorting & TZ-correct display
  unread?: number;
  atMe?: boolean;
  muted?: boolean;
  pinned?: boolean;
  order?: number;
  is_group: boolean;
  aggregateCategory?: "official" | "service";
  aggregateAvatars?: string[];
}

export interface SmartReplyRule {
  id: string;
  keyword: string;
  reply: string;
  use_regex: boolean;
  reply_with_matched_line: boolean;
}

export type SmartReplyAiOutputMode = "result" | "template" | "silent";

export interface SmartReplyAiTask {
  id: string;
  name: string;
  enabled: boolean;
  skill_type: "custom";
  skill_id: string;
  instruction: string;
  confidence: number;
  output_mode: SmartReplyAiOutputMode;
  reply_template: string;
  preserve_formatting: boolean;
  send_items_separately: boolean;
  max_parallel: number;
}

export interface AiProfile {
  id: string;
  name: string;
  configured: boolean;
  api_key_configured: boolean;
  api_key?: string;
  base_url: string;
  model: string;
}

export interface AiSettings {
  configured: boolean;
  api_key_configured: boolean;
  api_key?: string;
  base_url: string;
  model: string;
  active_profile_id: string;
  profiles: AiProfile[];
}

export interface AiAnalysisResult {
  matched: boolean;
  confidence: number;
  result: string;
  items: string[];
  reply: string;
}

export type SmartReplyMessageType =
  | "text"
  | "image"
  | "gif"
  | "voice"
  | "video"
  | "file"
  | "xml"
  | "system"
  | "recall"
  | "quote";

export interface SmartReplyConfig {
  chat_id: string;
  chat_name: string;
  avatar?: string;
  enabled: boolean;
  mention_only: boolean;
  use_no_src: boolean;
  message_types: SmartReplyMessageType[];
  target_senders: string[];
  rules: SmartReplyRule[];
  ai_tasks: SmartReplyAiTask[];
  reply_count?: number;
  last_triggered_at?: number;
  created_at?: number;
  updated_at?: number;
}

export interface SmartReplyTarget {
  wxid: string;
  name: string;
  avatar?: string;
}

export interface ContactProfile {
  wxid: string;
  name: string;
  avatar?: string;
  profile?: Record<string, any>;
}

export interface WeChatAccount {
  id: string;
  account_id?: string;
  wxid?: string;
  nickname?: string;
  avatar?: string;
  phone?: string;
  region?: string;
  signature?: string;
  wechat_account?: string;
  profile?: Record<string, any>;
  peer?: string;
  server_port?: string;
  api_host?: string;
  api_port?: number;
  transport?: "http" | "ws";
  connected?: boolean;
  port_alive?: boolean;
  probe_latency_ms?: number;
  probe_error?: string;
  connected_at?: number;
  last_seen_at?: number;
  pending?: number;
  initialized?: boolean;
  login_status?: string;
  login_message?: string;
  login_status_updated_at?: number;
  active?: boolean;
  source?: "hook" | "protocol";
  session_id?: string;
  protocol_state?: string;
  rdv?: string;
  start_port?: number;
  callback_url?: string;
  qrcode_ready?: boolean;
  saved_mapping?: boolean;
  proxy?: Record<string, any>;
}

// ─── Messages ────────────────────────────────────────────────────

// WeChat MSG.Type reference:
// 1 text, 3 image, 34 voice, 37 friend confirm, 40 POSSIBLEFRIEND_MSG,
// 42 contact card, 43 video, 47 animated sticker, 48 location,
// 49 app/share/file message (appmsg.type=57 means quote/refer message),
// 50 VOIPMSG, 51 init, 52 VOIPNOTIFY, 53 VOIPINVITE, 62 short video,
// 9999 SYSNOTICE, 10000 system, 10002 revoke.
export interface ChatMessage {
  id: string;           // msgsvrid or clientmsgid
  msgtype: string;
  time: string;
  timestamp?: number;   // Unix epoch seconds – preferred for TZ-correct display
  fromid: string;       // actual sender wxid (for groups: the member who sent it)
  toid: string;
  fromgid?: string;     // group wxid (only for group messages)
  fromtype?: string;    // "1" = private, "2" = group
  msg: string;          // actual message content (group prefix stripped)
  sendorrecv: string;   // "1" = self, "2" = received
  isSender?: number;    // from DB: 1 = self sent, 0 = received
  // Extra fields by type
  img_path?: string;
  db_image_id?: string;
  img_len?: number;
  bytesExtraHex?: string;  // hex(BytesExtra) for type 3 images — used to find local file
  video_path?: string;
  voice_len?: string;
  voice_hex?: string;
  voice_data?: string;
  voice_path?: string;
  clientmsgid?: string;
  gif_path?: string;
  file_path?: string;
  info?: string;
  msgsource?: string;
  atuserlist?: string | string[];
}

// ─── WebSocket Messages ──────────────────────────────────────────

export interface WSInitMessage {
  type: "init";
  data: {
    account_id?: string;
    self_info: any;
    contacts: any;
    sessions: any;
    last_messages: Record<string, {
      content: string;
      type: string;
      is_sender: number;
      time: number;
      sender_wxid?: string;
    }>;
    avatar_urls: Record<string, string>;
    contact_profiles?: Record<string, ContactProfile>;
    hydration_progress?: ContactHydrationProgress;
    session_cache?: Record<string, {
      wxid: string;
      lastMsg: string;
      lastTime: string;
      lastTimestamp: number;
      unread: number;
    }>;
  };
}

export interface WSCallbackMessage {
  type: "wechat_message";
  data: {
    account_id?: string;
    selfwxid?: string;
    sendorrecv: string;
    messages?: ChatMessage[];
    msglist?: any[];
    session_updates?: Array<{
      wxid: string;
      lastMsg: string;
      lastTime: string;
      lastTimestamp: number;
      unread: number;
    }>;
    contact_updates?: Record<string, ContactProfile>;
  };
}

export interface WSMessageSent {
  type: "message_sent";
  data: {
    account_id?: string;
    chat_id: string;
    message: ChatMessage;
    session_update?: {
      wxid: string;
      lastMsg: string;
      lastTime: string;
      lastTimestamp: number;
      unread: number;
    };
  };
}

export interface WSMarkRead {
  type: "mark_read";
  data: {
    wxid: string;
  };
}

export interface WSContactProfiles {
  type: "contact_profiles";
  data: {
    account_id?: string;
    members: Record<string, ContactProfile>;
  };
}

export interface ContactHydrationProgress {
  active?: boolean;
  phase?: string;
  batch?: number;
  total_batches?: number;
  processed?: number;
  total?: number;
  updated?: number;
  failed?: number;
  current_batch_count?: number;
  current_batch_updated?: number;
}

export interface WSContactsSnapshot {
  type: "contacts_snapshot";
  data: {
    account_id?: string;
    contacts: any;
    contact_profiles?: Record<string, ContactProfile>;
    hydration_progress?: ContactHydrationProgress;
  };
}

export interface WSContactsHydrationProgress {
  type: "contacts_hydration_progress";
  data: ContactHydrationProgress & {
    account_id?: string;
    owner_wxid?: string;
  };
}

export interface WSSmartReplyUpdated {
  type: "smart_reply_updated";
  data: {
    config?: SmartReplyConfig;
    chat_id?: string;
    deleted?: boolean;
  };
}

export type WSMessage =
  | WSInitMessage
  | WSCallbackMessage
  | WSMessageSent
  | WSMarkRead
  | WSContactProfiles
  | WSContactsSnapshot
  | WSContactsHydrationProgress
  | WSSmartReplyUpdated;
