import { useState, useEffect, useMemo, useRef, type ReactNode, type WheelEvent, type MouseEvent } from "react";
import type { ChatMessage } from "../types";
import { getImageUrl, getDbImageUrl, downloadImage, authQuery } from "../api";
import { replaceWechatEmojis } from "../utils/wechatEmoji";

interface MessageBubbleProps {
  message: ChatMessage;
  isSelf: boolean;
  selfWxid: string;
  isGroup: boolean;
  senderName?: string;
  avatarUrl?: string;
  onAvatarClick?: (wxid: string) => void;
  mobile?: boolean;
  dark?: boolean;
}

function parseRefermsg(xml: string) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, "text/xml");
    const title = doc.querySelector("title")?.textContent || "";
    const refermsg = doc.querySelector("refermsg");
    if (!refermsg) return { title, refer: null };
    return {
      title,
      refer: {
        type: refermsg.querySelector("type")?.textContent || "",
        svrid: refermsg.querySelector("svrid")?.textContent || "",
        displayname: refermsg.querySelector("displayname")?.textContent || "",
        content: refermsg.querySelector("content")?.textContent || "",
      },
    };
  } catch {
    return { title: "", refer: null };
  }
}

function parseLocation(xml: string) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, "text/xml");
    const loc = doc.querySelector("location");
    if (!loc) return null;
    return {
      x: loc.getAttribute("x") || "",
      y: loc.getAttribute("y") || "",
      poiname: loc.getAttribute("poiname") || "",
      label: loc.getAttribute("label") || "",
    };
  } catch {
    return null;
  }
}

function parseVoipMessage(xml: string) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml || "", "text/xml");
    const text = doc.getElementsByTagName("msg")[0]?.textContent?.trim() || "";
    const durationText = doc.getElementsByTagName("duration")[0]?.textContent || "";
    const duration = Number.parseInt(durationText, 10) || 0;
    return { text, duration };
  } catch {
    return { text: "", duration: 0 };
  }
}

function formatVoipDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return "";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `Duration: ${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

/**
 * Format message time for display above each bubble.
 * Prefers Unix timestamp (seconds) for timezone-correct display.
 * Today → "14:30", Yesterday → "昨天14:30", Older → "3月3日14:30"
 */
function formatMessageTime(timeStr: string | undefined, timestamp?: number): string {
  let d: Date | null = null;

  // Prefer Unix timestamp for correct timezone handling
  if (timestamp && timestamp > 0) {
    d = new Date(timestamp * 1000);
    if (isNaN(d.getTime())) d = null;
  }

  // Fallback to time string parsing
  if (!d && timeStr) {
    try {
      if (/^\d{4}[/-]\d{1,2}[/-]\d{1,2}/.test(timeStr)) {
        d = new Date(timeStr.replace(/\//g, "-"));
      } else {
        d = new Date(timeStr);
      }
      if (isNaN(d!.getTime())) d = null;
    } catch {
      d = null;
    }
  }

  if (!d) return "";

  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const timeOnly = `${hh}:${mm}`;

  if (d.toDateString() === now.toDateString()) return timeOnly;

  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `昨天${timeOnly}`;

  return `${d.getMonth() + 1}月${d.getDate()}日${timeOnly}`;
}

/**
 * Avatar component: shows image if URL available, letter fallback otherwise.
 * Uses unique key per wxid to prevent state leakage between different contacts.
 */
function ChatAvatar({ wxid, isSelf, name, avatarUrl }: {
  wxid: string; isSelf: boolean; name: string; avatarUrl?: string;
}) {
  const [imgError, setImgError] = useState(false);

  if (avatarUrl && !imgError) {
    return (
      <img
        src={avatarUrl}
        alt=""
        className="w-[40px] h-[40px] rounded-[4px] object-cover"
        onError={() => setImgError(true)}
        loading="lazy"
      />
    );
  }

  // Letter fallback
  const initial = name?.[0] || wxid?.slice(-2)?.[0] || "?";
  return (
    <div
      className={`w-[40px] h-[40px] rounded-[4px] flex items-center justify-center text-white text-[14px] font-medium ${
        isSelf ? "bg-[#60b044]" : "bg-[#576b95]"
      }`}
    >
      {initial}
    </div>
  );
}

/**
 * Renders a msgtype=47 emoji/sticker.
 * Extracts md5 + CDN URLs from the XML and loads the sticker via backend.
 * Falls back to CDN download for sticker packs not yet downloaded locally.
 */
function EmojiSticker({ msgXml }: { msgXml: string }) {
  const [failed, setFailed] = useState(false);

  // Extract md5, cdnurl, thumburl from the emoji XML
  const { md5, stickerUrl } = useMemo(() => {
    try {
      const parser = new DOMParser();
      const doc = parser.parseFromString(msgXml, "text/xml");
      const emoji = doc.querySelector("emoji");
      const hash = emoji?.getAttribute("md5") || emoji?.getAttribute("androidmd5") || "";
      const cdnurl = emoji?.getAttribute("cdnurl") || "";
      const thumburl = emoji?.getAttribute("thumburl") || "";

      // Build URL with CDN fallback params
      let url = "";
      if (hash) {
        const params = new URLSearchParams();
        if (cdnurl) params.set("cdnurl", cdnurl);
        if (thumburl) params.set("thumburl", thumburl);
        const keyQuery = authQuery();
        if (keyQuery) {
          new URLSearchParams(keyQuery).forEach((value, key) => params.set(key, value));
        }
        const qs = params.toString();
        url = `/api/media/sticker/${hash}${qs ? `?${qs}` : ""}`;
      }
      return { md5: hash, stickerUrl: url };
    } catch {
      return { md5: "", stickerUrl: "" };
    }
  }, [msgXml]);

  if (!md5 || failed) {
    return <span className="text-3xl">😊</span>;
  }

  return (
    <img
      src={stickerUrl}
      alt="表情"
      className="max-w-[120px] max-h-[120px]"
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

/**
 * ChatImage — handles both real-time images (img_path) and DB images (BytesExtra path lookup).
 * For DB images, sends BytesExtraHex to backend which finds the file on disk.
 */
const IMAGE_RETRY_DELAYS = [15, 30, 60]; // seconds between retries
const PREVIEW_MIN_SCALE = 0.35;
const PREVIEW_MAX_SCALE = 5;
const URL_REGEX = /((?:https?:\/\/|www\.)[^\s<>"']+)/gi;
const URL_TRAILING_PUNCTUATION = /[),.;!?，。！？、；：]+$/;

function clampPreviewScale(value: number): number {
  return Math.min(PREVIEW_MAX_SCALE, Math.max(PREVIEW_MIN_SCALE, value));
}

function linkHref(url: string): string {
  return /^https?:\/\//i.test(url) ? url : `https://${url}`;
}

function renderTextWithLinks(text: string, isSelf: boolean, mobile: boolean, dark: boolean) {
  const normalized = (text || "").replace(/\r\n/g, "\n");
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let index = 0;
  URL_REGEX.lastIndex = 0;

  let match: RegExpExecArray | null;
  while ((match = URL_REGEX.exec(normalized)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(replaceWechatEmojis(normalized.slice(lastIndex, match.index)));
    }

    const rawUrl = match[0];
    const punctMatch = rawUrl.match(URL_TRAILING_PUNCTUATION);
    const trailing = punctMatch?.[0] || "";
    const urlText = trailing ? rawUrl.slice(0, -trailing.length) : rawUrl;
    if (urlText) {
      nodes.push(
        <a
          key={`url-${index}`}
          href={linkHref(urlText)}
          target="_blank"
          rel="noreferrer"
          className={`break-all ${isSelf || (mobile && !dark) ? "text-[#576b95]" : "text-[#7ea6d9]"} hover:underline`}
          onClick={(event) => event.stopPropagation()}
        >
          {urlText}
        </a>,
      );
      index += 1;
    }
    if (trailing) {
      nodes.push(replaceWechatEmojis(trailing));
    }

    lastIndex = match.index + rawUrl.length;
  }

  if (lastIndex < normalized.length) {
    nodes.push(replaceWechatEmojis(normalized.slice(lastIndex)));
  }
  return nodes.length > 0 ? nodes : replaceWechatEmojis(normalized);
}

function ChatImage({ message, onEnlarge, compact = false }: {
  message: ChatMessage;
  onEnlarge: (url: string) => void;
  compact?: boolean;
}) {
  const [src, setSrc] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    if (message.db_image_id) {
      setSrc(getDbImageUrl(message.db_image_id));
      return;
    }
    if (message.img_path) {
      setSrc(getImageUrl(message.img_path));
      return;
    }
    // DB image: try local file via BytesExtraHex, fall back to CDN_Download_Pic via msg_xml
    const hexData = message.bytesExtraHex || "";
    const msgXml = message.msg || "";
    if (!hexData && !msgXml) {
      setFailed(true);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    downloadImage(hexData, msgXml, message.id).then((url) => {
      if (cancelled) return;
      if (url) {
        setSrc(url);
        setLoading(false);
      } else {
        // Download returned empty — schedule auto-retry if attempts remain
        if (retryCount < IMAGE_RETRY_DELAYS.length) {
          const delay = IMAGE_RETRY_DELAYS[retryCount];
          setLoading(true);
          setTimeout(() => {
            if (!cancelled) setRetryCount((c) => c + 1);
          }, delay * 1000);
        } else {
          setFailed(true);
          setLoading(false);
        }
      }
    }).catch(() => {
      if (cancelled) return;
      if (retryCount < IMAGE_RETRY_DELAYS.length) {
        const delay = IMAGE_RETRY_DELAYS[retryCount];
        setTimeout(() => {
          if (!cancelled) setRetryCount((c) => c + 1);
        }, delay * 1000);
      } else {
        setFailed(true);
        setLoading(false);
      }
    });
    return () => { cancelled = true; };
  }, [message.db_image_id, message.img_path, message.bytesExtraHex, message.msg, retryCount]);

  if (loading) {
    const retryMsg = retryCount > 0 ? ` (重试 ${retryCount}/${IMAGE_RETRY_DELAYS.length})` : "";
    return <div className="text-[#888] text-[13px] py-2">加载图片中...{retryMsg}</div>;
  }
  if (failed || !src) {
    return (
      <div
        className="text-[#888] text-[13px] cursor-pointer hover:text-[#aaa]"
        onClick={() => { setRetryCount(0); setFailed(false); setLoading(true); }}
        title="点击重试"
      >
        [图片加载失败 - 点击重试]
      </div>
    );
  }
  return (
    <img
      src={src}
      alt="图片"
      className={`${compact ? "max-w-[72px] max-h-[72px]" : "max-w-[200px] max-h-[200px]"} rounded-[4px] cursor-pointer object-cover`}
      loading="lazy"
      onClick={() => onEnlarge(src)}
      onError={() => setFailed(true)}
    />
  );
}

export default function MessageBubble({
  message, isSelf, selfWxid, isGroup, senderName, avatarUrl, onAvatarClick, mobile = false, dark = true,
}: MessageBubbleProps) {
  const msgtype = String(message.msgtype);
  const [enlargedImg, setEnlargedImg] = useState<string | null>(null);
  const [previewScale, setPreviewScale] = useState(1);
  const [previewOrigin, setPreviewOrigin] = useState("50% 50%");
  const [previewOffset, setPreviewOffset] = useState({ x: 0, y: 0 });
  const [isPreviewDragging, setIsPreviewDragging] = useState(false);
  const previewImageRef = useRef<HTMLImageElement | null>(null);
  const previewDragStartRef = useRef<{ startX: number; startY: number; offsetX: number; offsetY: number } | null>(null);
  const didPreviewDragRef = useRef(false);

  useEffect(() => {
    setPreviewScale(1);
    setPreviewOrigin("50% 50%");
    setPreviewOffset({ x: 0, y: 0 });
    setIsPreviewDragging(false);
    previewDragStartRef.current = null;
    didPreviewDragRef.current = false;
  }, [enlargedImg]);

  const closeImagePreview = () => {
    setEnlargedImg(null);
    setPreviewScale(1);
    setPreviewOrigin("50% 50%");
    setPreviewOffset({ x: 0, y: 0 });
    setIsPreviewDragging(false);
    previewDragStartRef.current = null;
    didPreviewDragRef.current = false;
  };

  const handleImagePreviewOverlayClick = (event: MouseEvent<HTMLDivElement>) => {
    if (didPreviewDragRef.current) {
      event.preventDefault();
      event.stopPropagation();
      didPreviewDragRef.current = false;
      return;
    }
    closeImagePreview();
  };

  const handleImagePreviewWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();

    const rect = previewImageRef.current?.getBoundingClientRect();
    if (rect && rect.width > 0 && rect.height > 0) {
      const x = ((event.clientX - rect.left) / rect.width) * 100;
      const y = ((event.clientY - rect.top) / rect.height) * 100;
      setPreviewOrigin(`${Math.min(100, Math.max(0, x))}% ${Math.min(100, Math.max(0, y))}%`);
    }

    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    setPreviewScale((current) => {
      const next = clampPreviewScale(current * factor);
      return Math.round(next * 100) / 100;
    });
  };

  const handleImagePreviewMouseDown = (event: MouseEvent<HTMLImageElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    previewDragStartRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      offsetX: previewOffset.x,
      offsetY: previewOffset.y,
    };
    didPreviewDragRef.current = false;
    setIsPreviewDragging(true);
  };

  const handleImagePreviewMouseMove = (event: MouseEvent<HTMLDivElement>) => {
    if (!previewDragStartRef.current) return;
    event.preventDefault();
    event.stopPropagation();
    const { startX, startY, offsetX, offsetY } = previewDragStartRef.current;
    const deltaX = event.clientX - startX;
    const deltaY = event.clientY - startY;
    if (Math.abs(deltaX) + Math.abs(deltaY) > 3) {
      didPreviewDragRef.current = true;
    }
    setPreviewOffset({
      x: offsetX + deltaX,
      y: offsetY + deltaY,
    });
  };

  const stopImagePreviewDragging = () => {
    previewDragStartRef.current = null;
    setIsPreviewDragging(false);
  };

  const renderContent = () => {
    switch (msgtype) {
      case "1":
        return (
          <div style={{ fontSize: 17, lineHeight: "20px", wordBreak: "break-word", whiteSpace: "pre-wrap", margin: 0, padding: 0 }}>
            {renderTextWithLinks(message.msg || "", isSelf, mobile, dark)}
          </div>
        );

      case "3":
        return <ChatImage message={message} onEnlarge={setEnlargedImg} />;

      case "34": {
        const dur = message.voice_len ? Math.ceil(parseInt(message.voice_len) / 1000) : 0;
        return (
          <div className="flex items-center gap-2 min-w-[60px]">
            <svg className={`w-4 h-4 ${isSelf ? "text-[#333]" : "text-white"}`} fill="currentColor" viewBox="0 0 24 24">
              <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z" />
            </svg>
            <span className="text-[17px]">{dur > 0 ? `${dur}"` : "语音"}</span>
          </div>
        );
      }

      case "42":
        try {
          const parser = new DOMParser();
          const doc = parser.parseFromString(message.msg, "text/xml");
          const msgEl = doc.querySelector("msg");
          const nickname = msgEl?.getAttribute("nickname") || "名片";
          const smallimg = msgEl?.getAttribute("smallheadimgurl") || "";
          return (
            <div className="w-[220px]">
              <div className="flex items-center gap-2 pb-2 border-b border-[#333]">
                {smallimg ? (
                  <img src={smallimg} className="w-10 h-10 rounded-[4px]" alt="" />
                ) : (
                  <div className="w-10 h-10 rounded-[4px] bg-[#60b044] flex items-center justify-center text-white text-sm">
                    {nickname[0]}
                  </div>
                )}
                <span className="text-[17px]">{nickname}</span>
              </div>
              <div className="text-[11px] text-[#888] mt-1.5">个人名片</div>
            </div>
          );
        } catch {
          return <p className="text-[17px]">[名片消息]</p>;
        }

      case "43":
        if (message.video_path) {
          return (
            <div className="relative">
              <video
                src={getImageUrl(message.video_path)}
                className="max-w-[200px] max-h-[200px] rounded-[4px]"
                controls
              />
            </div>
          );
        }
        return (
          <div className="flex items-center gap-2 text-[17px]">
            <span>🎬</span><span>[视频]</span>
          </div>
        );

      case "47":
        if (message.gif_path) {
          return (
            <img
              src={getImageUrl(message.gif_path)}
              alt="GIF"
              className="max-w-[120px] max-h-[120px] rounded-[4px]"
              loading="lazy"
            />
          );
        }
        return <EmojiSticker msgXml={message.msg} />;

      case "48": {
        const loc = parseLocation(message.msg);
        if (loc) {
          return (
            <div className="flex items-center gap-2 text-[17px]">
              <span className="text-lg">📍</span>
              <span>{loc.poiname || loc.label || "位置"}</span>
            </div>
          );
        }
        return <p className="text-[17px]">[位置消息]</p>;
      }

      case "49": {
        // If content is empty, show placeholder
        if (!message.msg || message.msg.trim() === "") {
          return <span className="text-[#888] text-[17px]">[消息]</span>;
        }
        try {
          const parser = new DOMParser();
          const doc = parser.parseFromString(message.msg, "text/xml");
          const appType = doc.querySelector("type")?.textContent || "";

          if (appType === "57") {
            const parsed = parseRefermsg(message.msg);
            const quotedImage = parsed.refer?.type === "3" && Boolean(parsed.refer.content);
            return (
              <div className="min-w-[220px] max-w-[420px]">
                <div className="whitespace-pre-wrap break-words text-[17px] leading-[1.4]">
                  {replaceWechatEmojis(parsed.title?.replace(/\r\n/g, "\n") || "")}
                </div>
                {parsed.refer && (
                  <div className={`mt-2 rounded-[3px] px-2.5 py-2 text-[13px] leading-[1.35] ${
                    isSelf
                      ? "bg-[#dff6d4] text-[#668060]"
                      : dark ? "bg-[#3a3a3a] text-[#aaa]" : "bg-[#f0f0f0] text-[#777]"
                  }`}>
                    {quotedImage ? (
                      <div className="flex items-center gap-2">
                        <span className="font-medium shrink-0 self-start">{parsed.refer.displayname}:</span>
                        <ChatImage
                          compact
                          message={{
                            ...message,
                            id: parsed.refer.svrid || message.id,
                            msgtype: "3",
                            msg: parsed.refer.content,
                            bytesExtraHex: "",
                            img_path: undefined,
                            db_image_id: undefined,
                          }}
                          onEnlarge={setEnlargedImg}
                        />
                      </div>
                    ) : (
                      <>
                        <span className="font-medium">{parsed.refer.displayname}: </span>
                        <span>{replaceWechatEmojis(parsed.refer.content?.substring(0, 60) || "")}</span>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          }

          if (appType === "6" || appType === "74") {
            const title = doc.querySelector("title")?.textContent || "文件";
            return (
              <div className="flex items-center gap-3 w-[220px]">
                <span className="text-2xl">📄</span>
                <span className="text-[17px] truncate">{title}</span>
              </div>
            );
          }

          if (appType === "5") {
            const title = doc.querySelector("title")?.textContent || "链接";
            return (
              <div className="flex items-center gap-2 text-[17px]">
                <span>🔗</span><span className="truncate max-w-[200px]">{title}</span>
              </div>
            );
          }

          if (appType === "33" || appType === "36") {
            const title = doc.querySelector("title")?.textContent || "小程序";
            return (
              <div className="flex items-center gap-2 text-[17px]">
                <span>🟢</span><span>[小程序] {title}</span>
              </div>
            );
          }

          const title = doc.querySelector("title")?.textContent || message.msg.substring(0, 50);
          return <p className="text-[17px]">{title}</p>;
        } catch {
          return <p className="text-[#888] text-[17px]">[应用消息]</p>;
        }
      }

      case "50": {
        const voip = parseVoipMessage(message.msg || "");
        const label = voip.text || formatVoipDuration(voip.duration) || "语音聊天";
        return (
          <div className="flex items-center gap-2 min-w-[120px] max-w-[360px]">
            <svg
              className="w-5 h-5 shrink-0"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              viewBox="0 0 24 24"
            >
              <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.91.32 1.8.59 2.65a2 2 0 0 1-.45 2.11L8 9.73a16 16 0 0 0 6.27 6.27l1.25-1.25a2 2 0 0 1 2.11-.45c.85.27 1.74.47 2.65.59A2 2 0 0 1 22 16.92Z" />
            </svg>
            <span className="text-[17px] leading-[22px] break-words">{label}</span>
          </div>
        );
      }

      case "10000":
      case "10002":
        return null;

      case "9994":
        return null;

      default:
        return <p className="text-[#888] text-[17px]">[消息类型: {msgtype}]</p>;
    }
  };

  // System messages — centered label
  if (msgtype === "10000" || msgtype === "10002") {
    return (
      <div className="flex justify-center py-2 px-4">
        <div className={`text-[12px] rounded px-2.5 py-1 ${dark ? "bg-[#1e1e1e] text-[#888]" : "bg-[#dedede] text-[#999]"}`}>
          {replaceWechatEmojis(extractSystemText(message.msg))}
        </div>
      </div>
    );
  }

  if (msgtype === "9994") return null;

  const content = renderContent();
  if (!content) return null;

  const noBubble = msgtype === "47" || msgtype === "3" || (msgtype === "43" && message.video_path);

  // Use fromid as the avatar key — for group messages this is the actual sender
  const avatarWxid = isSelf ? selfWxid : (message.fromid || "");

  return (
    <div className={`flex gap-[8px] ${isSelf ? "flex-row-reverse" : "flex-row"}`} style={{ marginBottom: mobile ? "4px" : "0px", padding: mobile ? "6px 10px" : "5px" }}>
      {/* Avatar — keyed by wxid to prevent state leakage */}
      <div className="shrink-0 mt-[2px]" key={avatarWxid}>
        <button
          type="button"
          className="block rounded-[4px] active:opacity-70"
          onClick={() => onAvatarClick?.(avatarWxid)}
        >
          <ChatAvatar
            wxid={avatarWxid}
            isSelf={isSelf}
            name={isSelf ? "我" : (senderName || message.fromid || "?")}
            avatarUrl={avatarUrl}
          />
        </button>
      </div>

      {/* Content */}
      <div className={`${mobile ? "max-w-[72%]" : "max-w-[65%]"} ${isSelf ? "items-end" : "items-start"} flex flex-col`} style={{ minWidth: 0, overflow: "hidden" }}>
        {/* Time label (always shown) + sender name (group, non-self only) */}
        {(() => {
          const timeLabel = formatMessageTime(message.time, message.timestamp);
          if (isGroup && !isSelf && senderName) {
            // Group, other: "14:30 Name"
            return (
              <div className="text-[12px] text-[#888] mb-0.5 px-0.5">
                {timeLabel ? `${timeLabel} ${senderName}` : senderName}
              </div>
            );
          }
          // DM (both sides) or group self: just time
          if (timeLabel) {
            return (
              <div className="text-[12px] text-[#888] mb-0.5 px-0.5">
                {timeLabel}
              </div>
            );
          }
          return null;
        })()}

        {noBubble ? (
          <div>{content}</div>
        ) : (
          <div
            className={`text-[17px] ${
              isSelf
                ? "bg-[#95ec69] text-[#111]"
                : dark ? "bg-[#2d2d2d] text-[#e0e0e0]" : "bg-white text-[#111]"
            }`}
            style={{
              padding: "8px 10px",
              borderRadius: "4px",
              maxWidth: "min(500px, 65vw)",
              overflowWrap: "break-word",
              wordBreak: "break-all",
            }}
          >
            {content}
          </div>
        )}
      </div>

      {/* Lightbox overlay — click to close */}
      {enlargedImg && (
        <div
          className="fixed inset-0 z-[9999] bg-black/90 flex items-center justify-center"
          onClick={handleImagePreviewOverlayClick}
          onWheel={handleImagePreviewWheel}
          onMouseMove={handleImagePreviewMouseMove}
          onMouseUp={stopImagePreviewDragging}
          onMouseLeave={stopImagePreviewDragging}
        >
          <div
            className="select-none"
            style={{
              cursor: isPreviewDragging ? "grabbing" : "grab",
              transform: `translate3d(${previewOffset.x}px, ${previewOffset.y}px, 0)`,
              willChange: "transform",
            }}
            onClick={(event) => {
              event.stopPropagation();
              didPreviewDragRef.current = false;
            }}
          >
            <img
              ref={previewImageRef}
              src={enlargedImg}
              alt=""
              className="max-w-[95vw] max-h-[90vh] object-contain"
              draggable={false}
              style={{
                transform: `scale(${previewScale})`,
                transformOrigin: previewOrigin,
                transition: isPreviewDragging ? "none" : "transform 80ms ease-out",
                willChange: "transform",
              }}
              onMouseDown={handleImagePreviewMouseDown}
              onDoubleClick={(event) => {
                event.stopPropagation();
                setPreviewScale(1);
                setPreviewOrigin("50% 50%");
                setPreviewOffset({ x: 0, y: 0 });
                didPreviewDragRef.current = false;
                stopImagePreviewDragging();
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function extractSystemText(msg: string): string {
  try {
    if (msg.includes("<pat>")) {
      const match = msg.match(/<template><!\[CDATA\[(.+?)\]\]><\/template>/);
      if (match) return match[1].replace(/\$\{[^}]+\}/g, "某人");
    }
    if (msg.includes("<revokemsg>")) {
      const match = msg.match(/<replacemsg><!\[CDATA\[(.+?)\]\]><\/replacemsg>/);
      if (match) return match[1];
    }
    return msg.replace(/<[^>]+>/g, "").trim().substring(0, 80) || "[系统消息]";
  } catch {
    return "[系统消息]";
  }
}
