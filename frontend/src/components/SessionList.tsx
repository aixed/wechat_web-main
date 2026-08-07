import { useEffect, useState } from "react";
import type { Session } from "../types";
import { DEFAULT_AVATAR_URL } from "../avatar";
import { searchLocalData, type GlobalSearchEntry, type GlobalSearchResult } from "../api";

export type SessionMenuAction = "pin" | "unpin" | "mark_unread" | "mute" | "unmute" | "smart_reply" | "delete";

interface SessionListProps {
  sessions: Session[];
  activeWxid?: string | null;
  onSelectChat: (wxid: string, seed?: Partial<Session>) => void;
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
 * If no URL or load error, use the shared neutral avatar.
 */
function Avatar({ session }: { session: Session }) {
  const [failedUrl, setFailedUrl] = useState("");
  const avatarUrl = session.avatar || "";
  const aggregateAvatars = (session.aggregateAvatars || []).slice(0, 4);
  const aggregateTiles = Array.from({ length: 4 }, (_, index) => aggregateAvatars[index] || DEFAULT_AVATAR_URL);

  return (
    <div className="relative w-[42px] h-[42px] shrink-0">
      {session.aggregateCategory ? (
        <div className={`grid h-full w-full grid-cols-2 gap-px overflow-hidden rounded-[5px] p-[2px] ${session.aggregateCategory === "official" ? "bg-[#1688f0]" : "bg-[#21a8f4]"}`}>
          {aggregateTiles.map((url, index) => (
            <img
              key={`${url}:${index}`}
              src={url || DEFAULT_AVATAR_URL}
              alt=""
              className="h-full min-h-0 w-full min-w-0 rounded-[1px] bg-white object-cover"
              onError={(event) => {
                event.currentTarget.onerror = null;
                event.currentTarget.src = DEFAULT_AVATAR_URL;
              }}
              loading="lazy"
            />
          ))}
        </div>
      ) : (
        <img
          src={avatarUrl && failedUrl !== avatarUrl ? avatarUrl : DEFAULT_AVATAR_URL}
          alt=""
          className="w-full h-full rounded-[5px] object-cover"
          onError={() => setFailedUrl(avatarUrl)}
          loading="lazy"
        />
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

const emptySearchResult = (query = ""): GlobalSearchResult => ({
  query,
  contacts: [],
  groups: [],
  messages: [],
});

function searchEntryName(entry: GlobalSearchEntry) {
  return entry.name || entry.remark || entry.nickname || entry.wxid;
}

function SearchAvatar({ entry }: { entry: GlobalSearchEntry }) {
  return (
    <Avatar
      session={{
        wxid: entry.wxid,
        nickname: searchEntryName(entry),
        avatar: entry.avatar || "",
        is_group: Boolean(entry.is_group),
      }}
    />
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
  const [query, setQuery] = useState("");
  const [searchResult, setSearchResult] = useState<GlobalSearchResult>(() => emptySearchResult());
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const dark = theme !== "light";
  const normalizedQuery = query.trim();

  useEffect(() => {
    if (!normalizedQuery) {
      setSearchResult(emptySearchResult());
      setSearching(false);
      setSearchError("");
      return;
    }

    const controller = new AbortController();
    setSearchResult(emptySearchResult(normalizedQuery));
    setSearching(true);
    setSearchError("");
    const timer = window.setTimeout(() => {
      searchLocalData(normalizedQuery, 30, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          if (
            !result
            || !Array.isArray(result.contacts)
            || !Array.isArray(result.groups)
            || !Array.isArray(result.messages)
          ) {
            throw new Error("invalid_search_response");
          }
          setSearchResult({
            query: normalizedQuery,
            contacts: result.contacts,
            groups: result.groups,
            messages: result.messages,
            source: result.source,
          });
        })
        .catch((error) => {
          if (controller.signal.aborted || error?.name === "AbortError") return;
          console.error("[GLOBAL_SEARCH]", error);
          setSearchResult(emptySearchResult(normalizedQuery));
          setSearchError("搜索失败，请稍后重试");
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 300);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [normalizedQuery]);

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
    if (session.aggregateCategory) return;
    setMenu({
      x: Math.max(8, Math.min(e.clientX, window.innerWidth - 188)),
      y: Math.max(8, Math.min(e.clientY, window.innerHeight - 256)),
      session,
    });
  };

  const runAction = (action: SessionMenuAction) => {
    if (!menu) return;
    onSessionAction(action, menu.session);
    setMenu(null);
  };

  const openSearchEntry = (entry: GlobalSearchEntry) => {
    onSelectChat(entry.wxid, {
      nickname: searchEntryName(entry),
      avatar: entry.avatar || "",
      is_group: Boolean(entry.is_group || entry.wxid.includes("@chatroom")),
    });
    setQuery("");
  };

  const resultCount = searchResult.contacts.length + searchResult.groups.length + searchResult.messages.length;

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
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className={`bg-transparent border-none outline-none text-[14px] ml-[6px] w-full min-w-0 ${dark ? "text-[#999] placeholder-[#5c5c5c]" : "text-[#333] placeholder-[#888]"}`}
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery("")}
              title="清空搜索"
              aria-label="清空搜索"
              className={`ml-[4px] h-[20px] w-[20px] shrink-0 rounded-full flex items-center justify-center ${dark ? "bg-[#555] text-[#222] hover:bg-[#666]" : "bg-[#b8b8b8] text-[#eee] hover:bg-[#aaa]"}`}
            >
              <svg className="h-[12px] w-[12px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden>
                <path strokeLinecap="round" d="m7 7 10 10M17 7 7 17" />
              </svg>
            </button>
          ) : null}
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

      {/* Session list / global search results */}
      <div className="session-list-scroll flex-1 overflow-y-auto overflow-x-hidden">
        {normalizedQuery ? (
          <div className="pb-[12px]">
            {searching && resultCount === 0 ? (
              <div className={`text-center text-[14px] mt-20 ${dark ? "text-[#666]" : "text-[#888]"}`}>正在搜索本地数据...</div>
            ) : null}
            {!searching && searchError ? (
              <div className={`text-center text-[14px] mt-20 ${dark ? "text-[#888]" : "text-[#777]"}`}>{searchError}</div>
            ) : null}
            {!searching && !searchError && resultCount === 0 ? (
              <div className={`text-center text-[14px] mt-20 ${dark ? "text-[#666]" : "text-[#888]"}`}>未找到相关联系人、群聊或聊天记录</div>
            ) : null}

            <SearchSection title="联系人" entries={searchResult.contacts} dark={dark} onOpen={openSearchEntry} />
            <SearchSection title="群聊" entries={searchResult.groups} dark={dark} onOpen={openSearchEntry} kind="group" />
            <SearchSection title="聊天记录" entries={searchResult.messages} dark={dark} onOpen={openSearchEntry} kind="message" />

            {searching && resultCount > 0 ? (
              <div className={`py-[10px] text-center text-[12px] ${dark ? "text-[#666]" : "text-[#999]"}`}>正在更新...</div>
            ) : null}
          </div>
        ) : (
        <>
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
                  ? (isActive
                    ? "bg-[#2f2f2f] hover:bg-[#2f2f2f]"
                    : session.pinned
                      ? "bg-[#232323] hover:bg-[#292929] active:bg-[#2d2d2d]"
                      : "hover:bg-[#242424] active:bg-[#2a2a2a]")
                  : (isActive
                    ? "bg-[#d0d0d0] hover:bg-[#d0d0d0]"
                    : session.pinned
                      ? "bg-[#d9d9d9] hover:bg-[#d3d3d3] active:bg-[#cecece]"
                      : "hover:bg-[#dedede] active:bg-[#d3d3d3]")
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
        </>
        )}
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
          <ContextMenuItem dark={dark} onClick={() => runAction("smart_reply")}>智能回复</ContextMenuItem>
          <div className={`h-px my-[4px] ${dark ? "bg-[#3a3a3a]" : "bg-[#e2e2e2]"}`} />
          <ContextMenuItem dark={dark} danger onClick={() => runAction("delete")}>删除聊天</ContextMenuItem>
        </div>
      )}
    </div>
  );
}

function SearchSection({
  title,
  entries,
  kind = "contact",
  dark,
  onOpen,
}: {
  title: string;
  entries: GlobalSearchEntry[];
  kind?: "contact" | "group" | "message";
  dark: boolean;
  onOpen: (entry: GlobalSearchEntry) => void;
}) {
  if (entries.length === 0) return null;

  return (
    <section>
      <div className={`h-[34px] px-[12px] flex items-end pb-[6px] text-[13px] ${dark ? "text-[#777] bg-[#161616]" : "text-[#888] bg-[#e3e2e2]"}`}>
        {title}
      </div>
      {entries.map((entry) => {
        let detail = entry.account ? `微信号：${entry.account}` : "";
        if (kind === "group") {
          const members = (entry.matched_members || []).slice(0, 2).map((member) => {
            const account = member.account ? `（微信号：${member.account}）` : "";
            return `${member.name || member.wxid || "群成员"}${account}`;
          });
          detail = members.length > 0 ? `包含：${members.join("、")}${(entry.matched_members || []).length > 2 ? "..." : ""}` : "群聊";
        } else if (kind === "message") {
          detail = `${Math.max(1, Number(entry.match_count) || 0)} 条相关聊天记录`;
        } else if (!detail && entry.wxid && !entry.wxid.startsWith("wxid_")) {
          detail = `微信号：${entry.wxid}`;
        }

        return (
          <button
            key={`${kind}:${entry.wxid}`}
            type="button"
            onClick={() => onOpen(entry)}
            className={`w-full min-h-[64px] flex items-center px-[10px] py-[8px] text-left transition-colors ${dark ? "hover:bg-[#262626] active:bg-[#303030]" : "hover:bg-[#d9d9d9] active:bg-[#d1d1d1]"}`}
          >
            <SearchAvatar entry={entry} />
            <span className="min-w-0 flex-1 ml-[12px]">
              <span className={`block truncate text-[16px] leading-[21px] ${dark ? "text-[#e5e5e5]" : "text-[#111]"}`}>{searchEntryName(entry)}</span>
              <span className={`block truncate mt-[3px] text-[13px] leading-[18px] ${dark ? "text-[#777]" : "text-[#777]"}`}>{detail}</span>
            </span>
          </button>
        );
      })}
    </section>
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
