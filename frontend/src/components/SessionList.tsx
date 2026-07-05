import { useEffect, useState } from "react";
import type { Session } from "../types";

export type SessionMenuAction = "pin" | "unpin" | "mark_unread" | "mute" | "unmute" | "delete";

interface SessionListProps {
  sessions: Session[];
  activeWxid?: string | null;
  onSelectChat: (wxid: string) => void;
  onSessionAction: (action: SessionMenuAction, session: Session) => void;
  onRefreshSessions: () => void;
  loading?: boolean;
  theme?: "dark" | "light";
}

function PinBadge() {
  return (
    <span className="absolute -left-[3px] -top-[3px] w-[15px] h-[15px] rounded-full bg-[#07c160] text-white shadow-sm flex items-center justify-center">
      <svg className="w-[9px] h-[9px]" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
        <path d="M14.8 2.8 21.2 9.2 18 10.4 14.3 14.1 14.9 19.3 13.4 20.8 9 16.4 4.2 21.2 2.8 19.8 7.6 15 3.2 10.6 4.7 9.1 9.9 9.7 13.6 6 14.8 2.8Z" />
      </svg>
    </span>
  );
}

/**
 * Session avatar — keyed by wxid to prevent React state reuse across items.
 * If no URL or load error → letter fallback (never inherits previous avatar).
 */
function Avatar({ session }: { session: Session }) {
  const [imgError, setImgError] = useState(false);
  const avatarUrl = session.avatar || "";
  const initial = session.nickname?.[0] || session.wxid?.[0] || "?";

  return (
    <div className="relative w-[42px] h-[42px] shrink-0">
      {avatarUrl && !imgError ? (
        <img
          src={avatarUrl}
          alt=""
          className="w-full h-full rounded-[5px] object-cover"
          onError={() => setImgError(true)}
          loading="lazy"
        />
      ) : (
        <div
          className={`w-full h-full rounded-[5px] flex items-center justify-center text-white text-[16px] font-medium ${
            session.is_group ? "bg-[#576b95]" : "bg-[#60b044]"
          }`}
        >
          {initial}
        </div>
      )}
      {session.pinned ? <PinBadge /> : null}
    </div>
  );
}

/** Mute icon (small speaker-off) */
function MuteIcon() {
  return (
    <svg className="w-[14px] h-[14px] text-[#666] shrink-0" fill="currentColor" viewBox="0 0 24 24">
      <path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.796 8.796 0 0021 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 003.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z" />
    </svg>
  );
}

export default function SessionList({
  sessions,
  activeWxid,
  onSelectChat,
  onSessionAction,
  onRefreshSessions,
  loading = false,
  theme = "dark",
}: SessionListProps) {
  const [menu, setMenu] = useState<{ x: number; y: number; session: Session } | null>(null);
  const dark = theme !== "light";

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menu]);

  const openContextMenu = (e: React.MouseEvent, session: Session) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({
      x: Math.min(e.clientX, window.innerWidth - 188),
      y: Math.min(e.clientY, window.innerHeight - 220),
      session,
    });
  };

  const runAction = (action: SessionMenuAction) => {
    if (!menu) return;
    onSessionAction(action, menu.session);
    setMenu(null);
  };

  return (
    <div className={`h-full w-full flex flex-col no-select ${dark ? "bg-[#191919]" : "bg-[#e9e8e8]"}`}>
      {/* Search bar */}
      <div className="px-[8px] pt-[8px] pb-[6px] shrink-0 flex items-center gap-[6px]">
        <div className={`min-w-0 flex-1 rounded-[6px] flex items-center pr-[8px] h-[34px] sessionlist-searchbar ${dark ? "bg-[#262626]" : "bg-[#dcdcdc]"}`}>
          <span aria-hidden style={{ width: 5 }} className="shrink-0" />
          <svg className={`w-[14px] h-[14px] shrink-0 ${dark ? "text-[#5c5c5c]" : "text-[#777]"}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text"
            placeholder="搜索"
            className={`bg-transparent border-none outline-none text-[14px] ml-[6px] w-full min-w-0 ${dark ? "text-[#999] placeholder-[#5c5c5c]" : "text-[#333] placeholder-[#888]"}`}
          />
        </div>
        <button
          type="button"
          onClick={onRefreshSessions}
          disabled={loading}
          title="刷新最近会话"
          className={`h-[34px] px-[9px] rounded-[6px] text-[13px] shrink-0 transition-colors ${
            dark
              ? "bg-[#262626] text-[#d8d8d8] hover:bg-[#303030] disabled:text-[#666]"
              : "bg-[#dcdcdc] text-[#333] hover:bg-[#d2d2d2] disabled:text-[#999]"
          }`}
        >
          {loading ? "刷新中" : "刷新"}
        </button>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 && (
          <div className={`text-center text-[14px] mt-20 ${dark ? "text-[#5c5c5c]" : "text-[#999]"}`}>
            {loading ? "正在获取最近会话..." : "暂无会话，点击刷新获取最近会话"}
          </div>
        )}
        {sessions.map((session) => {
          const isActive = session.wxid === activeWxid;
          return (
            <div
              key={session.wxid}
              onClick={() => onSelectChat(session.wxid)}
              onContextMenu={(e) => openContextMenu(e, session)}
              className={`flex items-center px-0 py-0 cursor-pointer transition-colors ${
                dark
                  ? (isActive ? "bg-[#2f2f2f] hover:bg-[#2f2f2f]" : "hover:bg-[#242424] active:bg-[#2a2a2a]")
                  : (isActive ? "bg-[#d0d0d0] hover:bg-[#d0d0d0]" : "hover:bg-[#dedede] active:bg-[#d3d3d3]")
              }`}
            >
              {/* Avatar — keyed to prevent React reuse issues */}
              <div
                key={session.wxid + "_avatar"}
                style={{ padding: "8px" }}
              >
                <Avatar session={session} />
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0 ml-[4px] pb-[10px] pr-[4px]">
                <div className="flex justify-between items-baseline">
                  <span className={`text-[16px] truncate font-normal leading-[21px] ${dark ? "text-[#e5e5e5]" : "text-[#111]"}`} style={{ paddingLeft: '3px' }}>
                    {session.nickname || session.wxid}
                  </span>
                  <span className="flex items-center gap-[4px] shrink-0 ml-[8px]">
                    {/* Mute icon (before time) */}
                    {session.muted && <MuteIcon />}
                    <span className={`text-[13px] leading-[21px] mr-[3px] ${dark ? "text-[#666666]" : "text-[#999]"}`}>
                      {session.lastTime || ""}
                    </span>
                  </span>
                </div>
                <div className="flex justify-between items-center mt-[3px]">
                  <span className={`text-[14px] truncate leading-[18px] ${dark ? "text-[#666666]" : "text-[#999]"}`} style={{ paddingLeft: '3px' }}>
                    {session.lastMsg || ""}
                  </span>
                  {/* Unread badge — only if NOT muted */}
                  {!session.muted && session.unread && session.unread > 0 ? (
                    <span className="min-w-[18px] h-[18px] rounded-full bg-[#f04040] text-white text-[11px] flex items-center justify-center shrink-0 px-[5px] ml-[6px]">
                      {session.unread > 99 ? "99+" : session.unread}
                    </span>
                  ) : null}
                  {/* Muted indicator dot (tiny gray dot instead of red badge) */}
                  {session.muted && session.unread && session.unread > 0 ? (
                    <span className="w-[8px] h-[8px] rounded-full bg-[#666] shrink-0 ml-[6px]" />
                  ) : null}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {menu && (
        <div
          className={`fixed z-[9999] w-[180px] border shadow-xl py-[4px] text-[14px] ${dark ? "bg-[#2a2a2a] text-[#eee] border-[#444]" : "bg-[#f8f8f8] text-[#111] border-[#cfcfcf]"}`}
          style={{ left: menu.x, top: menu.y }}
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <ContextMenuItem dark={dark} onClick={() => runAction(menu.session.pinned ? "unpin" : "pin")}>
            {menu.session.pinned ? "取消置顶" : "置顶"}
          </ContextMenuItem>
          <ContextMenuItem dark={dark} onClick={() => runAction("mark_unread")}>标记未读</ContextMenuItem>
          <ContextMenuItem dark={dark} onClick={() => runAction(menu.session.muted ? "unmute" : "mute")}>
            {menu.session.muted ? "开启新消息提醒" : "消息免打扰"}
          </ContextMenuItem>
          <div className={`h-px my-[4px] ${dark ? "bg-[#3a3a3a]" : "bg-[#e2e2e2]"}`} />
          <ContextMenuItem dark={dark} danger onClick={() => runAction("delete")}>删除聊天</ContextMenuItem>
        </div>
      )}
    </div>
  );
}

function ContextMenuItem({
  children,
  danger,
  dark,
  onClick,
}: {
  children: React.ReactNode;
  danger?: boolean;
  dark: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`block w-full h-[36px] px-[18px] text-left ${
        dark ? "hover:bg-[#373737] active:bg-[#404040]" : "hover:bg-[#e5e5e5] active:bg-[#dadada]"
      } ${
        danger ? (dark ? "text-[#f1f1f1]" : "text-[#222]") : (dark ? "text-[#eee]" : "text-[#111]")
      }`}
    >
      {children}
    </button>
  );
}
