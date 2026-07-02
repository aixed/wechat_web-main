import { useState, useRef, useEffect, useCallback } from "react";
import type { ChatMessage, ContactProfile, Session } from "../types";
import { sendText, getMessages, getOlderMessages, sendImageUpload, sendFileUpload } from "../api";
import MessageBubble from "./MessageBubble";

interface ChatAreaProps {
  session: Session;
  messages: ChatMessage[];
  selfWxid: string;
  onBack: () => void;
  onNewMessages: (wxid: string, msgs: ChatMessage[]) => void;
  avatarMap: Record<string, string>;
  contactMap: Record<string, string>;
  contactProfiles: Record<string, ContactProfile>;
  onRequestContactProfile: (wxids: string[]) => Promise<Record<string, ContactProfile>>;
  onInputChange?: (hasText: boolean) => void;
}

/* Group sender parsing removed — WeChat 4.x stores sender in BytesExtra, backend extracts it */

const TEXTAREA_BASE_HEIGHT = 86;
const TEXTAREA_MAX_HEIGHT = 124;

interface PendingImage {
  id: string;
  file: File;
  url: string;
}

function imageFilesFromClipboardData(data: DataTransfer | null): File[] {
  const files = Array.from(data?.files || []).filter((file) => file.type.startsWith("image/"));
  if (files.length > 0) return files;

  return Array.from(data?.items || [])
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file));
}

export default function ChatArea({
  session, messages, selfWxid, onBack, onNewMessages, avatarMap, contactMap,
  contactProfiles, onRequestContactProfile, onInputChange,
}: ChatAreaProps) {
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasMoreOlder, setHasMoreOlder] = useState(true);
  const [inputMode, setInputMode] = useState<"text" | "voice">("text");
  const [showPlusMenu, setShowPlusMenu] = useState(false);
  const [profileWxid, setProfileWxid] = useState<string | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState("");
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([]);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const loadedRef = useRef<Set<string>>(new Set());
  const albumInputRef = useRef<HTMLInputElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isInitialScroll = useRef(true);
  const loadingOlderRef = useRef(false);
  const pendingImagesRef = useRef<PendingImage[]>([]);
  const lastPasteRef = useRef<{ key: string; at: number }>({ key: "", at: 0 });
  // When true, suppress all auto-scroll-to-bottom behaviors (used during "load older" flow)
  const suppressAutoScrollRef = useRef(false);
  const prevScrollHeightRef = useRef(0);
  // During "initial settling" period after entering a chat, always scroll to
  // bottom when async content (images/stickers) finishes loading — regardless
  // of how far the scroll position drifts from the bottom.
  const initialSettlingRef = useRef(true);

  const isGroup = session.is_group;
  const canSend = input.trim().length > 0 || pendingImages.length > 0;

  useEffect(() => {
    pendingImagesRef.current = pendingImages;
  }, [pendingImages]);

  useEffect(() => {
    return () => {
      pendingImagesRef.current.forEach((image) => URL.revokeObjectURL(image.url));
    };
  }, []);

  useEffect(() => {
    setPendingImages((prev) => {
      prev.forEach((image) => URL.revokeObjectURL(image.url));
      return [];
    });
    onInputChange?.(input.trim().length > 0);
  }, [session.wxid]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleAvatarClick = useCallback(async (wxid: string) => {
    if (!wxid) return;
    setProfileWxid(wxid);
    setProfileError("");

    const existing = contactProfiles[wxid];
    if (existing?.profile && Object.keys(existing.profile).length > 0) {
      return;
    }

    setProfileLoading(true);
    try {
      await onRequestContactProfile([wxid]);
    } catch (err) {
      console.error("[PROFILE]", err);
      setProfileError("资料加载失败");
    } finally {
      setProfileLoading(false);
    }
  }, [contactProfiles, onRequestContactProfile]);

  // ─── Auto-scroll to bottom ──────────────────────────────────────
  const scrollToBottom = useCallback((instant?: boolean) => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({
        behavior: instant ? "instant" : "smooth",
      });
    }
  }, []);

  // Scroll on messages change
  useEffect(() => {
    if (messages.length === 0) return;
    // When older messages were prepended, restore scroll position instead of scrolling to bottom
    if (suppressAutoScrollRef.current) {
      const container = messagesContainerRef.current;
      if (container) {
        const prevH = prevScrollHeightRef.current;
        // Use rAF to wait for DOM paint
        requestAnimationFrame(() => {
          const newH = container.scrollHeight;
          container.scrollTop = newH - prevH;
          // Keep suppressing for a short time so ResizeObserver doesn't fight us
          setTimeout(() => { suppressAutoScrollRef.current = false; }, 300);
        });
      } else {
        suppressAutoScrollRef.current = false;
      }
      return;
    }
    // Use instant scroll on initial load, smooth for new messages
    // Use requestAnimationFrame to ensure DOM has painted before scrolling
    const raf = requestAnimationFrame(() => {
      scrollToBottom(isInitialScroll.current);
      isInitialScroll.current = false;
    });
    return () => cancelAnimationFrame(raf);
  }, [messages, scrollToBottom]);

  // Reset initial scroll flag when switching chats & start settling period
  useEffect(() => {
    isInitialScroll.current = true;
    initialSettlingRef.current = true;
    setHasMoreOlder(true);
    loadingOlderRef.current = false;
    // After 5 seconds, stop force-scrolling on every content resize
    const timer = setTimeout(() => {
      initialSettlingRef.current = false;
    }, 5000);
    return () => clearTimeout(timer);
  }, [session.wxid]);

  // If the user manually scrolls up during the settling period, respect that
  // and stop force-scrolling.
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    let lastScrollTop = container.scrollTop;

    const handleScroll = () => {
      // User scrolled upward significantly → stop force-scroll
      if (container.scrollTop < lastScrollTop - 30) {
        initialSettlingRef.current = false;
      }
      lastScrollTop = container.scrollTop;
    };

    container.addEventListener("scroll", handleScroll, { passive: true });
    return () => container.removeEventListener("scroll", handleScroll);
  }, [session.wxid]);

  // Re-scroll when async content (images/stickers) finishes loading and
  // changes the scroll height.  During the initial settling window we always
  // scroll; afterwards we only scroll if the user is already near the bottom.
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    let settleTimer: ReturnType<typeof setTimeout>;

    const shouldAutoScroll = () => {
      // Don't fight scroll restoration after loading older messages
      if (suppressAutoScrollRef.current) return false;
      // During settling period, always scroll to bottom
      if (initialSettlingRef.current) return true;
      // After settling, only scroll if already near the bottom
      const { scrollTop, scrollHeight, clientHeight } = container;
      return scrollHeight - scrollTop - clientHeight < 150;
    };

    const observer = new ResizeObserver(() => {
      if (!shouldAutoScroll()) return;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        scrollToBottom(true);
      }, 30);
    });

    // Observe the inner content wrapper (first child of the scroll container)
    const inner = container.firstElementChild;
    if (inner) observer.observe(inner);

    return () => {
      clearTimeout(settleTimer);
      observer.disconnect();
    };
  }, [session.wxid, scrollToBottom]);

  // ─── Load history when opening chat ─────────────────────────────
  useEffect(() => {
    const isFirstLoad = !loadedRef.current.has(session.wxid);
    loadedRef.current.add(session.wxid);

    // Always fetch from backend — not just the first time.
    // Messages sent from mobile/desktop WeChat don't trigger hook callbacks,
    // so we must re-fetch DB history every time the user enters a chat.
    if (isFirstLoad) setLoadingHistory(true);

    getMessages(session.wxid, 100)
      .then((data: any) => {
        if (data && Array.isArray(data.data) && data.data.length > 0) {
          // Debug: log first 3 rows for group chats
          if (isGroup) {
            console.log("[DB_DEBUG] first 3 rows:", JSON.stringify(data.data.slice(0, 3)).substring(0, 1000));
          }
          const historyMsgs: ChatMessage[] = data.data.map((row: any) => {
            // New backend format: message is already normalized
            if (row && typeof row === "object" && row.msgtype && row.fromid && row.id) {
              return {
                ...row,
                id: String(row.id),
                msgtype: String(row.msgtype || "1"),
                sendorrecv: String(row.sendorrecv || "2"),
                isSender: Number(row.isSender ?? (String(row.sendorrecv) === "1" ? 1 : 0)),
                msg: String(row.msg || ""),
                time: String(row.time || ""),
                timestamp: Number(row.timestamp || row.time_unix || 0),
                fromid: String(row.fromid || ""),
                toid: String(row.toid || ""),
                fromgid: String(row.fromgid || ""),
                fromtype: String(row.fromtype || (isGroup ? "2" : "1")),
              } as ChatMessage;
            }

            const isList = Array.isArray(row);
            const rawContent = String(isList ? (row[3] || "") : (row.StrContent || ""));
            const talker = String(isList ? (row[2] || "") : (row.StrTalker || ""));
            const isSenderVal = Number(isList ? (row[6] ?? 0) : (row.IsSender ?? 0));
            const isSenderBool = isSenderVal === 1;

            let fromid = "";
            // Normalize \r\n → \n (WeChat DB stores Windows line endings)
            const msgContent = rawContent.replace(/\r\n/g, "\n");

            if (isSenderBool) {
              fromid = selfWxid;
            } else if (isGroup) {
              // WeChat 4.x: sender wxid is extracted from BytesExtra by the backend
              const senderWxid = (row as any).SenderWxid || "";
              fromid = senderWxid || talker;
            } else {
              fromid = talker;
            }

            const msgType = String(isList ? (row[5] || "1") : (row.Type || "1"));
            const createTs = Number(isList ? (row[1] || 0) : (row.CreateTime || 0));
            return {
              id: String(isList ? (row[4] || Math.random()) : (row.MsgSvrID || Math.random())),
              msgtype: msgType,
              time: createTs ? new Date(createTs * 1000).toLocaleString("zh-CN") : "",
              timestamp: createTs,
              fromid,
              toid: isGroup ? "" : (isSenderBool ? session.wxid : selfWxid),
              fromgid: isGroup ? session.wxid : "",
              fromtype: isGroup ? "2" : "1",
              msg: msgContent,
              sendorrecv: isSenderBool ? "1" : "2",
              isSender: isSenderVal,
              // For type 3 (image), pass BytesExtraHex so frontend can resolve the local file
              bytesExtraHex: msgType === "3" ? ((row as any).BytesExtraHex || "") : undefined,
            };
          });

          // Backend returns messages in ascending chronological order (oldest first)
          if (historyMsgs.length > 0) {
            onNewMessages(session.wxid, historyMsgs);
          }
        }
      })
      .catch((err: Error) => console.error("[HISTORY]", err))
      .finally(() => setLoadingHistory(false));
  }, [session.wxid]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ─── Load older messages (scroll to top) ──────────────────────
  const loadOlderMessagesHandler = useCallback(async () => {
    if (loadingOlderRef.current || !hasMoreOlder || messages.length === 0) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);

    // Find the oldest message timestamp
    const oldestTs = messages.reduce(
      (min, m) => Math.min(min, m.timestamp || Infinity),
      Infinity
    );
    if (!oldestTs || oldestTs === Infinity) {
      setLoadingOlder(false);
      loadingOlderRef.current = false;
      return;
    }

    const container = messagesContainerRef.current;

    try {
      const data = await getOlderMessages(session.wxid, oldestTs, 50);
      if (data && Array.isArray(data.data) && data.data.length > 0) {
        const olderMsgs: ChatMessage[] = data.data.map((row: any) => ({
          ...row,
          id: String(row.id),
          msgtype: String(row.msgtype || "1"),
          sendorrecv: String(row.sendorrecv || "2"),
          isSender: Number(row.isSender ?? (String(row.sendorrecv) === "1" ? 1 : 0)),
          msg: String(row.msg || ""),
          time: String(row.time || ""),
          timestamp: Number(row.timestamp || row.time_unix || 0),
          fromid: String(row.fromid || ""),
          toid: String(row.toid || ""),
          fromgid: String(row.fromgid || ""),
          fromtype: String(row.fromtype || (isGroup ? "2" : "1")),
        }));

        // Save current scroll height and set suppress flag BEFORE triggering re-render
        prevScrollHeightRef.current = container?.scrollHeight || 0;
        suppressAutoScrollRef.current = true;
        onNewMessages(session.wxid, olderMsgs);

        if (data.data.length < 50) {
          setHasMoreOlder(false);
        }
      } else {
        setHasMoreOlder(false);
      }
    } catch (err) {
      console.error("[LOAD_OLDER]", err);
    } finally {
      setLoadingOlder(false);
      loadingOlderRef.current = false;
    }
  }, [messages, hasMoreOlder, session.wxid, isGroup, onNewMessages]);

  // Detect scroll to top → load older messages
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      // When scrolled near top (within 50px), trigger loading older messages
      if (container.scrollTop < 50 && !loadingOlderRef.current && hasMoreOlder) {
        loadOlderMessagesHandler();
      }
    };

    container.addEventListener("scroll", handleScroll, { passive: true });
    return () => container.removeEventListener("scroll", handleScroll);
  }, [loadOlderMessagesHandler, hasMoreOlder]);

  // ─── Send message ───────────────────────────────────────────────
  const handleSend = async () => {
    const imagesToSend = pendingImages;
    if ((!input.trim() && imagesToSend.length === 0) || sending) return;
    const msg = input.trim();
    setInput("");
    setPendingImages([]);
    onInputChange?.(false);
    setSending(true);

    if (textareaRef.current) {
      textareaRef.current.style.height = `${TEXTAREA_BASE_HEIGHT}px`;
    }

    try {
      if (msg) {
        await sendText(session.wxid, msg);
      }
      for (const image of imagesToSend) {
        await sendImageUpload(session.wxid, image.file);
      }
    } catch (err) {
      console.error("[SEND]", err);
    } finally {
      imagesToSend.forEach((image) => URL.revokeObjectURL(image.url));
      setSending(false);
      // Re-focus textarea so mobile keyboard stays open after sending
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
      });
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setInput(val);
    onInputChange?.(val.trim().length > 0 || pendingImages.length > 0);
    const el = e.target;
    el.style.height = `${TEXTAREA_BASE_HEIGHT}px`;
    el.style.height = Math.min(Math.max(el.scrollHeight, TEXTAREA_BASE_HEIGHT), TEXTAREA_MAX_HEIGHT) + "px";
  };

  const addPendingImages = useCallback((files: File[]) => {
    const imageFiles = files.filter((file) => file.type.startsWith("image/"));
    if (imageFiles.length === 0) return false;

    const key = imageFiles
      .map((file) => `${file.name}:${file.size}:${file.lastModified}:${file.type}`)
      .join("|");
    const now = Date.now();
    if (lastPasteRef.current.key === key && now - lastPasteRef.current.at < 500) {
      return true;
    }
    lastPasteRef.current = { key, at: now };

    setInputMode("text");
    setShowPlusMenu(false);
    setPendingImages((prev) => [
      ...prev,
      ...imageFiles.map((file, index) => ({
        id: `pending_img_${now}_${index}_${Math.random().toString(36).slice(2)}`,
        file,
        url: URL.createObjectURL(file),
      })),
    ]);
    onInputChange?.(true);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
    return true;
  }, [onInputChange]);

  const removePendingImage = useCallback((id: string) => {
    setPendingImages((prev) => {
      const target = prev.find((image) => image.id === id);
      if (target) URL.revokeObjectURL(target.url);
      const next = prev.filter((image) => image.id !== id);
      onInputChange?.(input.trim().length > 0 || next.length > 0);
      return next;
    });
  }, [input, onInputChange]);

  // ─── Plus-menu file handlers ──────────────────────────────────
  const sendImageFile = useCallback(async (file: File) => {
    if (!file || sending) return;
    setShowPlusMenu(false);
    setSending(true);
    try {
      await sendImageUpload(session.wxid, file);
    } catch (err) {
      console.error("[SEND_IMG]", err);
    } finally {
      setSending(false);
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
      });
    }
  }, [sending, session.wxid]);

  const handleImagePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";           // allow re-selecting the same file
    if (!file) return;
    await sendImageFile(file);
  };

  const handleImagePaste = useCallback((data: DataTransfer | null) => {
    const files = imageFilesFromClipboardData(data);
    return addPendingImages(files);
  }, [addPendingImages]);

  useEffect(() => {
    const handleWindowPaste = (e: ClipboardEvent) => {
      if (profileWxid) return;
      const files = imageFilesFromClipboardData(e.clipboardData);
      if (files.length === 0) return;

      e.preventDefault();
      handleImagePaste(e.clipboardData);
    };

    window.addEventListener("paste", handleWindowPaste);
    return () => window.removeEventListener("paste", handleWindowPaste);
  }, [handleImagePaste, profileWxid]);

  const handleFilePick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setShowPlusMenu(false);
    setSending(true);
    try {
      await sendFileUpload(session.wxid, file);
    } catch (err) {
      console.error("[SEND_FILE]", err);
    } finally {
      setSending(false);
    }
  };

  // ─── Render ─────────────────────────────────────────────────────
  return (
    <div className="h-full w-full flex flex-col bg-[#191919]">
      {/* ─── Top bar ─── */}
      <div className="h-[48px] px-[10px] flex items-center shrink-0 border-b border-[#2a2a2a] bg-[#191919] z-10">
        <button
          onClick={onBack}
          className="w-[36px] h-[36px] flex items-center justify-center text-[#e5e5e5]"
        >
          <svg className="w-[22px] h-[22px]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>
        <h2 className="flex-1 text-center text-[17px] font-medium text-[#e5e5e5] truncate pr-[36px]">
          {session.nickname || session.wxid}
        </h2>
        <button className="w-[36px] h-[36px] flex items-center justify-center text-[#e5e5e5]">
          <svg className="w-[20px] h-[20px]" fill="currentColor" viewBox="0 0 24 24">
            <circle cx="5" cy="12" r="1.5" />
            <circle cx="12" cy="12" r="1.5" />
            <circle cx="19" cy="12" r="1.5" />
          </svg>
        </button>
      </div>

      {/* ─── Messages area ─── */}
      {loadingHistory && messages.length === 0 ? (
        /* While fetching initial history, show a stable placeholder
           instead of an empty chat that flashes before messages appear */
        <div className="flex-1 bg-[#111111] flex items-end justify-center pb-6">
          <span className="text-[12px] text-[#5c5c5c]">加载历史消息...</span>
        </div>
      ) : (
        <div
          ref={messagesContainerRef}
          className="flex-1 overflow-y-auto min-h-0 bg-[#111111]"
        >
          <div className="py-2 pb-1">
            {/* Load older messages indicator */}
            {messages.length > 0 && (
              <div className="flex justify-center py-2">
                {loadingOlder ? (
                  <span className="text-[12px] text-[#5c5c5c]">加载更多消息...</span>
                ) : hasMoreOlder ? (
                  <button
                    onClick={loadOlderMessagesHandler}
                    className="text-[12px] text-[#5c5c5c] hover:text-[#888] active:text-[#888]"
                  >
                    ↑ 查看更早的消息
                  </button>
                ) : (
                  <span className="text-[12px] text-[#3a3a3a]">— 没有更多消息了 —</span>
                )}
              </div>
            )}
            {messages.map((msg) => {
              const isSelf = Number(msg.isSender) === 1 ||
                (msg.sendorrecv === "1" && msg.msgtype !== "9994");

              // Determine sender wxid for avatar / name lookup
              const senderWxid = isSelf ? selfWxid : (msg.fromid || "");
              const senderName = contactMap[senderWxid] || senderWxid;
              const senderAvatarUrl = avatarMap[senderWxid] || "";

              return (
                <MessageBubble
                  key={msg.id}
                  message={msg}
                  isSelf={isSelf}
                  selfWxid={selfWxid}
                  isGroup={isGroup}
                  senderName={senderName}
                  avatarUrl={isSelf ? (avatarMap[selfWxid] || "") : senderAvatarUrl}
                  onAvatarClick={handleAvatarClick}
                />
              );
            })}
            {/* Scroll anchor */}
            <div ref={messagesEndRef} className="h-[1px]" />
          </div>
        </div>
      )}

      {/* ─── Bottom input bar ─── */}
      <div className="shrink-0 min-h-[176px] border-t border-[#2a2a2a] bg-[#1e1e1e] px-[16px] py-[8px] pb-[max(8px,env(safe-area-inset-bottom))]">
        <div className="h-[34px] flex items-center justify-between">
          <div className="flex items-center gap-[14px]">
            {/* Voice/Keyboard toggle */}
            <button
              onClick={() => { setInputMode(inputMode === "text" ? "voice" : "text"); setShowPlusMenu(false); }}
              className="w-[30px] h-[30px] flex items-center justify-center text-[#d6d6d6] active:text-[#fff]"
            >
              {inputMode === "text" ? (
                <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
                </svg>
              ) : (
                <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H12" />
                </svg>
              )}
            </button>

            <button className="w-[30px] h-[30px] flex items-center justify-center text-[#d6d6d6] active:text-[#fff]">
              <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.182 15.182a4.5 4.5 0 01-6.364 0M21 12a9 9 0 11-18 0 9 9 0 0118 0zM9.75 9.75c0 .414-.168.75-.375.75S9 10.164 9 9.75 9.168 9 9.375 9s.375.336.375.75zm-.375 0h.008v.015h-.008V9.75zm5.625 0c0 .414-.168.75-.375.75s-.375-.336-.375-.75.168-.75.375-.75.375.336.375.75zm-.375 0h.008v.015h-.008V9.75z" />
              </svg>
            </button>

            <button
              onClick={() => albumInputRef.current?.click()}
              className="w-[30px] h-[30px] flex items-center justify-center text-[#d6d6d6] active:text-[#fff]"
            >
              <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <circle cx="8.5" cy="8.5" r="1.5" />
                <path d="m21 15-5-5L5 21" />
              </svg>
            </button>

            <button
              onClick={() => fileInputRef.current?.click()}
              className="w-[30px] h-[30px] flex items-center justify-center text-[#d6d6d6] active:text-[#fff]"
            >
              <svg className="w-[23px] h-[23px]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
            </button>
          </div>

          <button
            onClick={() => setShowPlusMenu((v) => !v)}
            className={`w-[30px] h-[30px] flex items-center justify-center transition-transform ${showPlusMenu ? "text-[#07c160]" : "text-[#d6d6d6] active:text-[#fff]"}`}
          >
            <svg
              className={`w-[25px] h-[25px] transition-transform duration-200 ${showPlusMenu ? "rotate-45" : ""}`}
              fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
          </button>
        </div>

        {pendingImages.length > 0 && (
          <div className="mt-[8px] flex gap-[8px] overflow-x-auto pb-[2px]">
            {pendingImages.map((image) => (
              <div key={image.id} className="relative w-[74px] h-[74px] shrink-0 rounded-[4px] overflow-hidden bg-[#111] border border-[#333]">
                <img
                  src={image.url}
                  alt=""
                  className="w-full h-full object-cover"
                  draggable={false}
                />
                <button
                  type="button"
                  onClick={() => removePendingImage(image.id)}
                  className="absolute top-[3px] right-[3px] w-[20px] h-[20px] rounded-full bg-black/70 text-white text-[16px] leading-[18px] flex items-center justify-center"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Input area */}
        {inputMode === "text" ? (
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            onFocus={() => setShowPlusMenu(false)}
            rows={4}
            className="mt-[4px] block w-full bg-transparent text-white text-[15px] resize-none outline-none h-[86px] min-h-[86px] max-h-[124px] leading-[22px] placeholder-[#5c5c5c] overflow-y-auto"
            placeholder=""
            style={{ height: `${TEXTAREA_BASE_HEIGHT}px` }}
          />
        ) : (
          <button className="mt-[4px] w-full h-[86px] text-[#999] text-[15px] flex items-center justify-center active:bg-[#252525]">
            按住 说话
          </button>
        )}

        <div className="mt-[6px] h-[34px] flex items-center justify-end">
          <button
            onMouseDown={(e) => e.preventDefault()}
            onClick={handleSend}
            disabled={sending || !canSend}
            className={`min-w-[92px] h-[32px] px-[18px] rounded-[4px] text-[14px] transition-colors ${
              canSend && !sending
                ? "bg-[#07c160] text-white active:bg-[#06ad56]"
                : "bg-[#2a2a2a] text-[#666]"
            }`}
          >
            {sending ? "发送中" : "发送"}
          </button>
        </div>

        {/* ─── Plus menu panel ─── */}
        {showPlusMenu && (
          <div className="mt-[8px] pb-[4px]">
            <div className="grid grid-cols-4 gap-y-[16px] gap-x-[8px] px-[4px]">
              {/* Row 1 */}
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>}
                label="相册"
                onClick={() => albumInputRef.current?.click()}
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z"/><path d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Z"/></svg>}
                label="拍照"
                onClick={() => cameraInputRef.current?.click()}
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9a2.25 2.25 0 0 0-2.25-2.25h-9A2.25 2.25 0 0 0 2.25 7.5v9a2.25 2.25 0 0 0 2.25 2.25Z"/></svg>}
                label="视频通话"
                disabled
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 0 0 2.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 0 1-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102a1.125 1.125 0 0 0-1.091-.852H4.5A2.25 2.25 0 0 0 2.25 4.5v2.25Z"/></svg>}
                label="语音通话"
                disabled
              />

              {/* Row 2 */}
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/><path d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1 1 15 0Z"/></svg>}
                label="位置"
                disabled
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"/></svg>}
                label="文件"
                onClick={() => fileInputRef.current?.click()}
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z"/></svg>}
                label="联系人"
                disabled
              />
              <PlusMenuItem
                icon={<svg className="w-[28px] h-[28px]" fill="none" stroke="currentColor" strokeWidth={1.3} viewBox="0 0 24 24"><path d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z"/></svg>}
                label="转账"
                disabled
              />
            </div>
          </div>
        )}
      </div>

      {/* Hidden file inputs */}
      <input ref={albumInputRef}  type="file" accept="image/*"          className="hidden" onChange={handleImagePick} />
      <input ref={cameraInputRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={handleImagePick} />
      <input ref={fileInputRef}   type="file"                           className="hidden" onChange={handleFilePick} />

      {profileWxid && (
        <ContactProfileCard
          wxid={profileWxid}
          profile={contactProfiles[profileWxid]}
          fallbackName={contactMap[profileWxid] || profileWxid}
          fallbackAvatar={avatarMap[profileWxid] || ""}
          loading={profileLoading}
          error={profileError}
          onClose={() => setProfileWxid(null)}
        />
      )}
    </div>
  );
}

// ─── Plus menu item component ─────────────────────────────────────
function PlusMenuItem({ icon, label, onClick, disabled }: {
  icon: React.ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      className={`flex flex-col items-center gap-[6px] ${disabled ? "opacity-40" : "active:opacity-60"}`}
    >
      <div className={`w-[56px] h-[56px] rounded-[12px] bg-[#2d2d2d] flex items-center justify-center text-[#e5e5e5] ${
        disabled ? "" : "active:bg-[#3a3a3a]"
      }`}>
        {icon}
      </div>
      <span className="text-[11px] text-[#999] leading-[14px]">{label}</span>
    </button>
  );
}

function ContactProfileCard({
  wxid,
  profile,
  fallbackName,
  fallbackAvatar,
  loading,
  error,
  onClose,
}: {
  wxid: string;
  profile?: ContactProfile;
  fallbackName: string;
  fallbackAvatar: string;
  loading: boolean;
  error: string;
  onClose: () => void;
}) {
  const raw = profile?.profile || {};
  const remark = profileField(raw, ["Remark", "remark", "markname"]);
  const nickName = profileField(raw, ["NickName", "nickname", "nick"]);
  const name = remark || profile?.name || nickName || fallbackName || wxid;
  const avatar = profile?.avatar || raw.SmallHeadImgUrl || raw.BigHeadImgUrl || fallbackAvatar || "";
  const alias = profileField(raw, ["Alias", "alias"]);
  const phone = profileField(raw, ["tel", "Tel", "phone", "Phone", "mobile", "Mobile", "MobileFullHash"]);
  const labelText = profileField(raw, ["LabelText", "LabelName", "LabelNames", "labelText", "labelname"]);
  const sign = profileField(raw, ["SignInfo", "signInfo", "sign"]);
  const roomCount = Number(raw.RoomInfoCount || 0);
  const sourceText = sourceLabel(raw.Source ?? raw.source);
  const area = formatArea(raw);
  const sex = Number(raw.Sex || 0);
  const showNickname = Boolean(nickName && nickName !== name);

  return (
    <div
      className="fixed inset-0 z-[9998] bg-black/45 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[420px] bg-[#f7f7f7] text-[#111] rounded-[2px] shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-[36px] pt-[34px] pb-[20px]">
          <div className="flex items-start gap-[22px]">
            {avatar ? (
              <img
                src={avatar}
                alt=""
                className="w-[88px] h-[88px] rounded-[8px] object-cover bg-[#ddd] shrink-0"
              />
            ) : (
              <div className="w-[88px] h-[88px] rounded-[8px] bg-[#576b95] flex items-center justify-center text-white text-[28px] shrink-0">
                {(name || wxid)[0] || "?"}
              </div>
            )}
            <div className="min-w-0 flex-1 pt-[2px]">
              <div className="flex items-center gap-[7px] min-w-0">
                <h3 className="text-[24px] leading-[30px] font-medium truncate">{name}</h3>
                {sex > 0 && <SexIcon sex={sex} />}
              </div>
              {showNickname && (
                <div className="mt-[7px] text-[16px] leading-[24px] text-[#888] truncate">
                  昵称：{nickName}
                </div>
              )}
              <div className="mt-[7px] text-[16px] leading-[24px] text-[#888] truncate">
                微信号：{alias || wxid}
              </div>
              {area && (
                <div className="text-[16px] leading-[24px] text-[#888] truncate">
                  地区：{area}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="w-[28px] h-[28px] shrink-0 text-[#777] flex items-center justify-center active:opacity-60"
              aria-label="关闭"
            >
              <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          </div>

          <div className="h-px bg-[#e6e6e6] my-[28px]" />

          {remark && <ProfileRow label="备注" value={remark} />}
          {phone && <ProfileRow label="电话" value={phone} />}
          {labelText && <ProfileRow label="标签" value={labelText} />}

          <div className="h-px bg-[#e6e6e6] my-[24px]" />

          <ProfileRow label="共同群聊" value={`${roomCount}个`} />
          {sign && <ProfileRow label="个性签名" value={sign} />}
          {sourceText && <ProfileRow label="来源" value={sourceText} />}
          <ProfileRow label="wxid" value={wxid} mono />

          {(loading || error) && (
            <div className={`mt-[18px] text-[13px] ${error ? "text-[#c44545]" : "text-[#888]"}`}>
              {error || "正在加载资料..."}
            </div>
          )}
        </div>

        <div className="h-px bg-[#e6e6e6]" />
        <div className="grid grid-cols-3 h-[104px] bg-white">
          <ProfileAction
            label="发消息"
            icon={<path d="M4 6.5A3.5 3.5 0 0 1 7.5 3h9A3.5 3.5 0 0 1 20 6.5v5A3.5 3.5 0 0 1 16.5 15H11l-5 4v-4.35A3.5 3.5 0 0 1 4 11.5v-5Z" />}
            onClick={onClose}
          />
          <ProfileAction
            label="语音聊天"
            icon={<path d="M6.6 4.2 9 3.6l2 4-1.5 1.5a10.2 10.2 0 0 0 5.4 5.4L16.4 13l4 2-.6 2.4c-.2.8-.9 1.4-1.8 1.3C10.7 18.2 5.8 13.3 5.3 6c-.1-.9.5-1.6 1.3-1.8Z" />}
          />
          <ProfileAction
            label="视频聊天"
            icon={<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h7A2.5 2.5 0 0 1 16 6.5v7A2.5 2.5 0 0 1 13.5 16h-7A2.5 2.5 0 0 1 4 13.5v-7Zm12.5 2.2 3.5-2.1v6.8l-3.5-2.1V8.7Z" />}
          />
        </div>
      </div>
    </div>
  );
}

function ProfileRow({ label, value, muted, mono }: {
  label: string;
  value: string;
  muted?: boolean;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-[12px] py-[3px] text-[18px] leading-[28px]">
      <div className="text-[#8a8a8a]">{label}</div>
      <div className={`${muted ? "text-[#999]" : "text-[#111]"} ${mono ? "font-mono text-[14px] leading-[24px]" : ""} break-words`}>
        {value}
      </div>
    </div>
  );
}

function ProfileAction({ icon, label, onClick }: {
  icon: React.ReactNode;
  label: string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col items-center justify-center gap-[8px] text-[#576b95] active:bg-[#f2f2f2]"
    >
      <svg className="w-[31px] h-[31px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
        {icon}
      </svg>
      <span className="text-[14px]">{label}</span>
    </button>
  );
}

function SexIcon({ sex }: { sex: number }) {
  const color = sex === 2 ? "#e96f92" : "#1e9bf0";
  return (
    <svg className="w-[18px] h-[18px] shrink-0" viewBox="0 0 24 24" fill={color}>
      <circle cx="12" cy="7" r="4" />
      <path d="M4.8 21c.8-4.2 3.2-6.3 7.2-6.3s6.4 2.1 7.2 6.3H4.8Z" />
    </svg>
  );
}

function profileField(raw: Record<string, any>, keys: string[]): string {
  for (const key of keys) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function formatArea(raw: Record<string, any>): string {
  const country = String(raw.Country || "").trim();
  const province = String(raw.Province || "").trim();
  const area = String(raw.Area || "").trim();
  const displayCountry = country && country !== "CN" ? country : "";
  return [displayCountry, area, province].filter(Boolean).join(" ");
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
