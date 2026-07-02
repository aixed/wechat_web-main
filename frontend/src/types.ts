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
  muted?: boolean;
  pinned?: boolean;
  order?: number;
  is_group: boolean;
}

export interface ContactProfile {
  wxid: string;
  name: string;
  avatar?: string;
  profile?: Record<string, any>;
}

// ─── Messages ────────────────────────────────────────────────────

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
  img_len?: number;
  bytesExtraHex?: string;  // hex(BytesExtra) for type 3 images — used to find local file
  video_path?: string;
  voice_len?: string;
  voice_hex?: string;
  voice_data?: string;
  gif_path?: string;
  file_path?: string;
  info?: string;
  msgsource?: string;
}

// ─── WebSocket Messages ──────────────────────────────────────────

export interface WSInitMessage {
  type: "init";
  data: {
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
    messages_cache?: Record<string, ChatMessage[]>;
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

export type WSMessage = WSInitMessage | WSCallbackMessage | WSMessageSent | WSMarkRead;
