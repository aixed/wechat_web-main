import { useCallback, useEffect, useMemo, useState } from "react";
import { deleteSmartReply, getGroupMemberDetails, getSmartReplies, saveSmartReply } from "../api";
import type { SmartReplyConfig, SmartReplyMessageType, SmartReplyRule, SmartReplyTarget } from "../types";

interface GroupMember {
  wxid: string;
  name: string;
  avatar?: string;
}

interface SmartReplyManagerProps {
  theme: "dark" | "light";
  initialTarget?: SmartReplyTarget | null;
  availableTargets: SmartReplyTarget[];
  mobile?: boolean;
}

const MESSAGE_TYPE_OPTIONS: Array<{ value: SmartReplyMessageType; label: string }> = [
  { value: "text", label: "文本消息" },
  { value: "image", label: "图片消息" },
  { value: "gif", label: "GIF 消息" },
  { value: "voice", label: "语音消息" },
  { value: "video", label: "视频消息" },
  { value: "file", label: "文件消息" },
  { value: "xml", label: "XML 消息" },
  { value: "system", label: "系统消息" },
  { value: "recall", label: "撤回消息" },
  { value: "quote", label: "引用消息" },
];

function makeRule(): SmartReplyRule {
  const suffix = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return {
    id: `rule_${suffix}`,
    keyword: "",
    reply: "",
    use_regex: false,
    reply_with_matched_line: false,
  };
}

function makeDraft(target: SmartReplyTarget): SmartReplyConfig {
  return {
    chat_id: target.wxid,
    chat_name: target.name || target.wxid,
    avatar: target.avatar || "",
    enabled: true,
    message_types: ["text"],
    target_senders: [],
    rules: [makeRule()],
    reply_count: 0,
    last_triggered_at: 0,
  };
}

function cloneConfig(config: SmartReplyConfig): SmartReplyConfig {
  return {
    ...config,
    message_types: config.message_types?.length ? [...config.message_types] : ["text"],
    target_senders: [...(config.target_senders || [])],
    rules: (config.rules || []).map((rule) => ({
      ...rule,
      use_regex: Boolean(rule.use_regex),
      reply_with_matched_line: Boolean(rule.reply_with_matched_line),
    })),
  };
}

function parseImportedRules(value: unknown): SmartReplyRule[] {
  const rawRules = Array.isArray(value)
    ? value
    : value && typeof value === "object" && Array.isArray((value as { rules?: unknown }).rules)
      ? (value as { rules: unknown[] }).rules
      : null;
  if (!rawRules || rawRules.length === 0) {
    throw new Error("JSON 中没有可导入的规则");
  }
  if (rawRules.length > 100) {
    throw new Error("一次最多导入 100 条规则");
  }

  return rawRules.map((value, index) => {
    if (!value || typeof value !== "object") {
      throw new Error(`第 ${index + 1} 条规则格式错误`);
    }
    const item = value as Record<string, unknown>;
    const keyword = String(item.keyword || "").trim();
    const reply = String(item.reply || "").trim();
    const useRegex = Boolean(item.use_regex);
    const replyWithMatchedLine = Boolean(item.reply_with_matched_line);
    if (!keyword || (!replyWithMatchedLine && !reply)) {
      throw new Error(`第 ${index + 1} 条规则缺少关键词或回复内容`);
    }
    if (keyword.length > 200 || reply.length > 4000) {
      throw new Error(`第 ${index + 1} 条规则内容过长`);
    }
    return {
      ...makeRule(),
      keyword,
      reply,
      use_regex: useRegex,
      reply_with_matched_line: replyWithMatchedLine,
    };
  });
}

function errorText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => String(item?.msg || item)).join("；");
  return "操作失败";
}

function formatTriggeredAt(timestamp?: number): string {
  if (!timestamp) return "尚未触发";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

export default function SmartReplyManager({
  theme,
  initialTarget,
  availableTargets,
  mobile = false,
}: SmartReplyManagerProps) {
  const dark = theme === "dark";
  const [configs, setConfigs] = useState<SmartReplyConfig[]>([]);
  const [draft, setDraft] = useState<SmartReplyConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [membersLoading, setMembersLoading] = useState(false);
  const [members, setMembers] = useState<GroupMember[]>([]);
  const [memberQuery, setMemberQuery] = useState("");
  const [listQuery, setListQuery] = useState("");
  const [pickerQuery, setPickerQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [activeMessageType, setActiveMessageType] = useState<SmartReplyMessageType>("text");
  const [expandedRuleId, setExpandedRuleId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!expandedRuleId) return;
    const collapseOnOutsideClick = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) return;
      const ruleElement = event.target.closest("[data-smart-reply-rule-id]");
      if (ruleElement?.getAttribute("data-smart-reply-rule-id") !== expandedRuleId) {
        setExpandedRuleId(null);
      }
    };
    document.addEventListener("pointerdown", collapseOnOutsideClick);
    return () => document.removeEventListener("pointerdown", collapseOnOutsideClick);
  }, [expandedRuleId]);

  const loadMembers = useCallback(async (chatId: string) => {
    setMembersLoading(true);
    setMembers([]);
    setMemberQuery("");
    try {
      const data = await getGroupMemberDetails(chatId);
      const rawMembers = data?.members && typeof data.members === "object"
        ? data.members as Record<string, { name?: unknown; avatar?: unknown }>
        : {};
      const rows = Object.entries(rawMembers).map(([wxid, item]) => ({
        wxid,
        name: String(item?.name || wxid),
        avatar: String(item?.avatar || ""),
      }));
      rows.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
      setMembers(rows);
    } catch {
      setError("群成员加载失败");
    } finally {
      setMembersLoading(false);
    }
  }, []);

  const openConfig = useCallback((config: SmartReplyConfig) => {
    setDraft(cloneConfig(config));
    setActiveMessageType("text");
    setExpandedRuleId(null);
    setError("");
    setNotice("");
    loadMembers(config.chat_id);
  }, [loadMembers]);

  const openTarget = useCallback((target: SmartReplyTarget, rows = configs) => {
    const existing = rows.find((config) => config.chat_id === target.wxid);
    openConfig(existing || makeDraft(target));
    setPickerOpen(false);
  }, [configs, openConfig]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError("");
      try {
        const data = await getSmartReplies();
        if (cancelled) return;
        const rows = Array.isArray(data?.configs) ? data.configs as SmartReplyConfig[] : [];
        setConfigs(rows);
        if (initialTarget?.wxid) {
          const existing = rows.find((config) => config.chat_id === initialTarget.wxid);
          openConfig(existing || makeDraft(initialTarget));
        } else if (rows.length > 0) {
          openConfig(rows[0]);
        } else {
          setDraft(null);
        }
      } catch {
        if (!cancelled) setError("智能回复配置加载失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [initialTarget, openConfig]);

  const save = async () => {
    if (!draft || saving) return;
    const rules = draft.rules.map((rule) => ({
      ...rule,
      keyword: rule.keyword.trim(),
      reply: rule.reply.trim(),
    }));
    if (draft.target_senders.length === 0) {
      setError("请选择至少一位目标发送人");
      return;
    }
    if (draft.message_types.length === 0) {
      setError("请至少选择一种消息类型");
      return;
    }
    if (rules.length === 0 || rules.some((rule) => !rule.keyword || (!rule.reply_with_matched_line && !rule.reply))) {
      setError("请完整填写关键词和回复内容");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const data = await saveSmartReply(draft.chat_id, {
        chat_name: draft.chat_name,
        avatar: draft.avatar,
        enabled: draft.enabled,
        message_types: draft.message_types,
        target_senders: draft.target_senders,
        rules,
      });
      if (!data?.ok || !data?.config) {
        setError(errorText(data?.detail || data?.error));
        return;
      }
      const saved = data.config as SmartReplyConfig;
      setDraft(cloneConfig(saved));
      setConfigs((prev) => [saved, ...prev.filter((config) => config.chat_id !== saved.chat_id)]);
      setNotice("已保存");
      window.setTimeout(() => setNotice(""), 1800);
    } catch {
      setError("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!draft || !configs.some((config) => config.chat_id === draft.chat_id)) return;
    if (!window.confirm(`删除“${draft.chat_name}”的智能回复配置？`)) return;
    setSaving(true);
    setError("");
    try {
      const data = await deleteSmartReply(draft.chat_id);
      if (!data?.ok) {
        setError(errorText(data?.detail || data?.error));
        return;
      }
      const remaining = configs.filter((config) => config.chat_id !== draft.chat_id);
      setConfigs(remaining);
      if (remaining.length > 0) openConfig(remaining[0]);
      else setDraft(null);
    } catch {
      setError("删除失败");
    } finally {
      setSaving(false);
    }
  };

  const updateDraft = (patch: Partial<SmartReplyConfig>) => {
    setDraft((prev) => prev ? { ...prev, ...patch } : prev);
    setNotice("");
  };

  const updateRule = (id: string, patch: Partial<SmartReplyRule>) => {
    setDraft((prev) => prev ? {
      ...prev,
      rules: prev.rules.map((rule) => rule.id === id ? { ...rule, ...patch } : rule),
    } : prev);
  };

  const importRules = async (file: File) => {
    if (!draft) return;
    if (file.size > 5 * 1024 * 1024) {
      setError("JSON 文件不能超过 5 MB");
      return;
    }
    try {
      const imported = parseImportedRules(JSON.parse(await file.text()));
      const onlyBlankRule = draft.rules.length === 1
        && !draft.rules[0].keyword.trim()
        && !draft.rules[0].reply.trim();
      const rules = onlyBlankRule ? imported : [...draft.rules, ...imported];
      if (rules.length > 100) {
        setError("规则总数不能超过 100 条");
        return;
      }
      updateDraft({ rules });
      setExpandedRuleId(null);
      setError("");
      setNotice(`已导入 ${imported.length} 条规则，请保存`);
    } catch (value) {
      setError(value instanceof Error ? value.message : "规则 JSON 解析失败");
    }
  };

  const exportRules = () => {
    if (!draft) return;
    const payload = {
      version: 1,
      chat_id: draft.chat_id,
      chat_name: draft.chat_name,
      rules: draft.rules.map((rule) => ({
        keyword: rule.keyword,
        reply: rule.reply,
        use_regex: Boolean(rule.use_regex),
        reply_with_matched_line: Boolean(rule.reply_with_matched_line),
      })),
    };
    const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const safeName = (draft.chat_name || draft.chat_id).replace(/[\\/:*?"<>|]/g, "_").slice(0, 60);
    anchor.href = url;
    anchor.download = `${safeName || "smart-reply"}-rules.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const toggleSender = (wxid: string) => {
    if (!draft) return;
    const selected = new Set(draft.target_senders);
    if (selected.has(wxid)) selected.delete(wxid);
    else selected.add(wxid);
    updateDraft({ target_senders: Array.from(selected) });
  };

  const toggleMessageType = (messageType: SmartReplyMessageType) => {
    if (!draft) return;
    const selected = new Set(draft.message_types);
    if (selected.has(messageType)) selected.delete(messageType);
    else selected.add(messageType);
    updateDraft({
      message_types: MESSAGE_TYPE_OPTIONS
        .map((option) => option.value)
        .filter((value) => selected.has(value)),
    });
  };

  const memberRows = useMemo(() => {
    const byId = new Map(members.map((member) => [member.wxid, member]));
    for (const wxid of draft?.target_senders || []) {
      if (!byId.has(wxid)) byId.set(wxid, { wxid, name: wxid });
    }
    const query = memberQuery.trim().toLocaleLowerCase();
    return Array.from(byId.values()).filter((member) =>
      !query || member.name.toLocaleLowerCase().includes(query) || member.wxid.toLocaleLowerCase().includes(query)
    );
  }, [draft?.target_senders, memberQuery, members]);

  const filteredConfigs = useMemo(() => {
    const query = listQuery.trim().toLocaleLowerCase();
    return configs.filter((config) =>
      !query || config.chat_name.toLocaleLowerCase().includes(query) || config.chat_id.toLocaleLowerCase().includes(query)
    );
  }, [configs, listQuery]);

  const availablePickerTargets = useMemo(() => {
    const query = pickerQuery.trim().toLocaleLowerCase();
    return availableTargets
      .filter((target) => !query || target.name.toLocaleLowerCase().includes(query) || target.wxid.toLocaleLowerCase().includes(query))
      .slice(0, 300);
  }, [availableTargets, pickerQuery]);

  const activeMessageTypeOption = MESSAGE_TYPE_OPTIONS.find((option) => option.value === activeMessageType)
    || MESSAGE_TYPE_OPTIONS[0];

  const listPane = (
    <div className={`h-full flex flex-col ${dark ? "bg-[#191919]" : "bg-[#e9e8e8]"}`}>
      <div className={`h-[62px] px-[14px] flex items-center border-b ${dark ? "border-[#2a2a2a]" : "border-[#d7d7d7]"}`}>
        {mobile ? (
          <div className={`w-[38px] ${dark ? "text-[#aaa]" : "text-[#555]"}`} />
        ) : null}
        <h1 className={`text-[17px] font-medium flex-1 ${mobile ? "text-center" : ""}`}>智能回复</h1>
        <button
          type="button"
          title="添加群聊"
          onClick={() => setPickerOpen(true)}
          className={`w-[34px] h-[34px] flex items-center justify-center rounded-[5px] ${dark ? "text-[#bbb] hover:bg-[#292929]" : "text-[#444] hover:bg-[#d8d8d8]"}`}
        >
          <svg className="w-[20px] h-[20px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" d="M12 5v14M5 12h14" />
          </svg>
        </button>
      </div>
      <div className="p-[10px]">
        <div className={`h-[34px] rounded-[6px] flex items-center px-[10px] ${dark ? "bg-[#262626]" : "bg-[#dcdcdc]"}`}>
          <svg className={`w-[15px] h-[15px] shrink-0 ${dark ? "text-[#666]" : "text-[#888]"}`} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" d="m21 21-5-5m2-6a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" />
          </svg>
          <input
            value={listQuery}
            onChange={(event) => setListQuery(event.target.value)}
            placeholder="搜索群聊"
            className={`ml-[7px] min-w-0 flex-1 bg-transparent outline-none text-[13px] ${dark ? "text-[#ddd] placeholder:text-[#666]" : "text-[#222] placeholder:text-[#888]"}`}
          />
        </div>
      </div>
      <div className="pane-scroll flex-1 min-h-0 overflow-y-auto">
        {loading ? (
          <div className={`py-[48px] text-center text-[13px] ${dark ? "text-[#666]" : "text-[#999]"}`}>加载中...</div>
        ) : filteredConfigs.length === 0 ? (
          <div className={`py-[48px] px-[20px] text-center text-[13px] ${dark ? "text-[#666]" : "text-[#999]"}`}>暂无智能回复配置</div>
        ) : filteredConfigs.map((config) => {
          const active = config.chat_id === draft?.chat_id;
          return (
            <button
              type="button"
              key={config.chat_id}
              onClick={() => openConfig(config)}
              className={`w-full h-[66px] px-[12px] flex items-center gap-[10px] text-left ${
                dark
                  ? (active ? "bg-[#303030]" : "hover:bg-[#242424]")
                  : (active ? "bg-[#d0d0d0]" : "hover:bg-[#dedede]")
              }`}
            >
              <TargetAvatar target={{ wxid: config.chat_id, name: config.chat_name, avatar: config.avatar }} size={42} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-[7px]">
                  <span className="truncate text-[15px]">{config.chat_name || config.chat_id}</span>
                  <span className={`w-[7px] h-[7px] rounded-full shrink-0 ${config.enabled ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#aaa]")}`} />
                </div>
                <div className={`mt-[3px] text-[12px] truncate ${dark ? "text-[#707070]" : "text-[#888]"}`}>
                  {config.target_senders.length} 人 · {config.rules.length} 条规则
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );

  const editorPane = draft ? (
    <div className={`h-full flex flex-col ${dark ? "bg-[#111] text-[#e8e8e8]" : "bg-[#f5f5f5] text-[#111]"}`}>
      <div className={`h-[62px] px-[18px] border-b flex items-center gap-[12px] ${dark ? "border-[#292929] bg-[#171717]" : "border-[#ddd] bg-white"}`}>
        {mobile && (
          <button type="button" title="返回" onClick={() => setDraft(null)} className={`w-[34px] h-[34px] flex items-center justify-center ${dark ? "text-[#bbb]" : "text-[#444]"}`}>
            <svg className="w-[21px] h-[21px]" fill="none" stroke="currentColor" strokeWidth={1.9} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="m15 18-6-6 6-6" />
            </svg>
          </button>
        )}
        <TargetAvatar target={{ wxid: draft.chat_id, name: draft.chat_name, avatar: draft.avatar }} size={38} />
        <div className="min-w-0 flex-1">
          <div className="text-[16px] font-medium truncate">{draft.chat_name || draft.chat_id}</div>
          <div className={`text-[11px] truncate mt-[1px] ${dark ? "text-[#666]" : "text-[#999]"}`}>{draft.chat_id}</div>
        </div>
        {notice && <span className="text-[12px] text-[#07c160] shrink-0">{notice}</span>}
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="h-[34px] px-[15px] rounded-[5px] bg-[#07c160] text-white text-[13px] disabled:bg-[#315541]"
        >
          {saving ? "保存中" : "保存"}
        </button>
      </div>

      <div className="pane-scroll flex-1 min-h-0 overflow-y-auto">
        <div className="w-full max-w-[980px] mx-auto px-[clamp(16px,4vw,48px)] py-[24px]">
          {error && <div className={`mb-[18px] px-[12px] py-[9px] border text-[13px] rounded-[5px] ${dark ? "border-[#5d3333] bg-[#281919] text-[#f0a0a0]" : "border-[#efc4c4] bg-[#fff2f2] text-[#b74242]"}`}>{error}</div>}

          <section className={`pb-[24px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
            <div className="flex items-center justify-between gap-[18px]">
              <div>
                <h2 className="text-[15px] font-medium">启用状态</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>
                  {draft.enabled ? "监听中" : "已停止"} · 已回复 {draft.reply_count || 0} 次 · {formatTriggeredAt(draft.last_triggered_at)}
                </div>
              </div>
              <Toggle checked={draft.enabled} onChange={(enabled) => updateDraft({ enabled })} />
            </div>
          </section>

          <section className={`py-[24px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
            <div>
              <h2 className="text-[15px] font-medium">消息类别</h2>
              <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>已启用 {draft.message_types.length} 类</div>
            </div>
            <div role="tablist" className={`mt-[14px] flex flex-wrap items-center gap-x-[20px] gap-y-[4px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
              {MESSAGE_TYPE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="tab"
                  aria-selected={activeMessageType === option.value}
                  onClick={() => {
                    setActiveMessageType(option.value);
                    setExpandedRuleId(null);
                  }}
                  className={`h-[38px] flex items-center gap-[7px] border-b-2 text-[13px] ${
                    activeMessageType === option.value
                      ? "border-[#07c160] text-[#07c160]"
                      : `border-transparent ${dark ? "text-[#aaa] hover:text-white" : "text-[#666] hover:text-black"}`
                  }`}
                >
                  <span>{option.label}</span>
                  <span className={`w-[6px] h-[6px] rounded-full ${draft.message_types.includes(option.value) ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#bbb]")}`} />
                </button>
              ))}
            </div>
          </section>

          <section className={`py-[24px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
            <div className="flex items-center justify-between gap-[18px]">
              <div>
                <h2 className="text-[15px] font-medium">{activeMessageTypeOption.label}</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>
                  {draft.message_types.includes(activeMessageType) ? "已启用" : "未启用"}
                </div>
              </div>
              <Toggle
                checked={draft.message_types.includes(activeMessageType)}
                onChange={() => toggleMessageType(activeMessageType)}
              />
            </div>
          </section>

          {activeMessageType === "text" ? (
            <>
          <section className={`py-[24px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
            <div className="flex items-center justify-between gap-[12px]">
              <div>
                <h2 className="text-[15px] font-medium">目标发送人</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>已选择 {draft.target_senders.length} 人</div>
              </div>
              {members.length > 0 && (
                <button
                  type="button"
                  onClick={() => updateDraft({
                    target_senders: draft.target_senders.length === members.length ? [] : members.map((member) => member.wxid),
                  })}
                  className={`text-[13px] ${dark ? "text-[#9a9a9a] hover:text-white" : "text-[#666] hover:text-black"}`}
                >
                  {draft.target_senders.length === members.length ? "取消全选" : "全选"}
                </button>
              )}
            </div>
            <div className={`mt-[14px] h-[36px] max-w-[420px] rounded-[5px] border flex items-center px-[10px] ${dark ? "border-[#353535] bg-[#1b1b1b]" : "border-[#d5d5d5] bg-white"}`}>
              <svg className={`w-[15px] h-[15px] ${dark ? "text-[#666]" : "text-[#888]"}`} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" d="m21 21-5-5m2-6a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" />
              </svg>
              <input value={memberQuery} onChange={(event) => setMemberQuery(event.target.value)} placeholder="搜索群成员" className="ml-[7px] min-w-0 flex-1 bg-transparent outline-none text-[13px]" />
            </div>
            {membersLoading ? (
              <div className={`py-[22px] text-[13px] ${dark ? "text-[#666]" : "text-[#999]"}`}>正在加载群成员...</div>
            ) : (
              <div className="mt-[12px] grid grid-cols-1 xl:grid-cols-2 gap-x-[28px] max-h-[280px] overflow-y-auto pane-scroll">
                {memberRows.map((member) => {
                  const checked = draft.target_senders.includes(member.wxid);
                  return (
                    <label key={member.wxid} className={`h-[52px] flex items-center gap-[10px] border-b cursor-pointer ${dark ? "border-[#252525]" : "border-[#e5e5e5]"}`}>
                      <input type="checkbox" checked={checked} onChange={() => toggleSender(member.wxid)} className="w-[16px] h-[16px] accent-[#07c160]" />
                      <TargetAvatar target={member} size={34} />
                      <div className="min-w-0 flex-1">
                        <div className="text-[13px] truncate">{member.name}</div>
                        <div className={`text-[10px] truncate mt-[1px] ${dark ? "text-[#606060]" : "text-[#aaa]"}`}>{member.wxid}</div>
                      </div>
                    </label>
                  );
                })}
              </div>
            )}
          </section>

          <section className="py-[24px]">
            <div className="flex flex-wrap items-start justify-between gap-[10px]">
              <div>
                <h2 className="text-[15px] font-medium">关键词规则</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>{draft.rules.length} 条</div>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-[8px]">
                <label className={`h-[32px] px-[11px] inline-flex items-center rounded-[5px] border text-[13px] cursor-pointer ${dark ? "border-[#3b3b3b] hover:bg-[#222]" : "border-[#d2d2d2] bg-white hover:bg-[#f0f0f0]"}`}>
                  导入规则
                  <input
                    type="file"
                    accept="application/json,.json"
                    className="hidden"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      event.target.value = "";
                      if (file) void importRules(file);
                    }}
                  />
                </label>
                <button
                  type="button"
                  onClick={exportRules}
                  className={`h-[32px] px-[11px] rounded-[5px] border text-[13px] ${dark ? "border-[#3b3b3b] hover:bg-[#222]" : "border-[#d2d2d2] bg-white hover:bg-[#f0f0f0]"}`}
                >
                  导出规则
                </button>
                <button
                  type="button"
                  onClick={() => updateDraft({ rules: [...draft.rules, makeRule()] })}
                  className={`h-[32px] px-[11px] rounded-[5px] border text-[13px] ${dark ? "border-[#3b3b3b] hover:bg-[#222]" : "border-[#d2d2d2] bg-white hover:bg-[#f0f0f0]"}`}
                >
                  添加规则
                </button>
              </div>
            </div>
            <div className="mt-[14px] space-y-[10px]">
              {draft.rules.map((rule, index) => (
                <div
                  key={rule.id}
                  data-smart-reply-rule-id={rule.id}
                  className={`rounded-[6px] border p-[14px] ${dark ? "border-[#303030] bg-[#181818]" : "border-[#dcdcdc] bg-white"}`}
                >
                  <div className="flex items-center gap-[10px]">
                    <span className={`text-[12px] w-[22px] shrink-0 ${dark ? "text-[#666]" : "text-[#999]"}`}>{index + 1}</span>
                    <input
                      value={rule.keyword}
                      onChange={(event) => updateRule(rule.id, { keyword: event.target.value })}
                      maxLength={200}
                      placeholder="关键词"
                      className={`h-[36px] min-w-0 flex-1 rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111]" : "border-[#d8d8d8] bg-[#fafafa]"}`}
                    />
                    <button
                      type="button"
                      title="删除规则"
                      disabled={draft.rules.length <= 1}
                      onClick={() => updateDraft({ rules: draft.rules.filter((item) => item.id !== rule.id) })}
                      className={`w-[34px] h-[34px] flex items-center justify-center disabled:opacity-25 ${dark ? "text-[#888] hover:text-[#e57373]" : "text-[#777] hover:text-[#c33]"}`}
                    >
                      <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5" />
                      </svg>
                    </button>
                  </div>
                  {expandedRuleId === rule.id ? (
                    <div className="mt-[10px]">
                      <div className={`min-h-[38px] flex flex-wrap items-center gap-x-[22px] gap-y-[8px] rounded-[5px] border px-[10px] py-[8px] text-[13px] ${dark ? "border-[#393939] bg-[#111] text-[#ccc]" : "border-[#d8d8d8] bg-[#fafafa] text-[#333]"}`}>
                        <label className="flex items-center gap-[7px] cursor-pointer select-none">
                          <input
                            type="checkbox"
                            checked={rule.use_regex}
                            onChange={(event) => updateRule(rule.id, { use_regex: event.target.checked })}
                            className="w-[16px] h-[16px] accent-[#07c160]"
                          />
                          <span>使用正则表达式</span>
                        </label>
                        <label className="flex items-center gap-[7px] cursor-pointer select-none">
                          <input
                            type="checkbox"
                            checked={rule.reply_with_matched_line}
                            onChange={(event) => updateRule(rule.id, { reply_with_matched_line: event.target.checked })}
                            className="w-[16px] h-[16px] accent-[#07c160]"
                          />
                          <span>发送命中行（去除末尾数字）</span>
                        </label>
                      </div>
                      {!rule.reply_with_matched_line && (
                        <textarea
                          autoFocus
                          value={rule.reply}
                          onChange={(event) => updateRule(rule.id, { reply: event.target.value })}
                          maxLength={4000}
                          rows={5}
                          placeholder="回复内容"
                          className={`mt-[10px] block w-full h-[120px] resize-none overflow-y-auto rounded-[5px] border px-[10px] py-[8px] leading-[20px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111]" : "border-[#d8d8d8] bg-[#fafafa]"}`}
                        />
                      )}
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setExpandedRuleId(rule.id)}
                      className={`mt-[10px] block w-full h-[38px] rounded-[5px] border px-[10px] text-left text-[14px] leading-[36px] truncate ${dark ? "border-[#393939] bg-[#111] text-[#ddd]" : "border-[#d8d8d8] bg-[#fafafa] text-[#222]"}`}
                    >
                      {rule.reply_with_matched_line
                        ? "发送命中行（去除末尾数字）"
                        : rule.reply || <span className={dark ? "text-[#666]" : "text-[#999]"}>回复内容</span>}
                    </button>
                  )}
                </div>
              ))}
            </div>
          </section>
            </>
          ) : (
            <section className={`py-[56px] text-center border-b ${dark ? "border-[#292929] text-[#666]" : "border-[#ddd] text-[#999]"}`}>
              <div className="text-[13px]">暂无配置项</div>
            </section>
          )}

          {configs.some((config) => config.chat_id === draft.chat_id) && (
            <div className={`pt-[20px] border-t flex justify-end ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
              <button type="button" disabled={saving} onClick={remove} className="h-[34px] px-[12px] text-[13px] text-[#d95757] disabled:opacity-40">删除配置</button>
            </div>
          )}
        </div>
      </div>
    </div>
  ) : (
    <div className={`h-full flex items-center justify-center ${dark ? "bg-[#111] text-[#666]" : "bg-[#f5f5f5] text-[#999]"}`}>
      <div className="text-center">
        <svg className="w-[38px] h-[38px] mx-auto mb-[12px]" fill="none" stroke="currentColor" strokeWidth={1.4} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 5.5h14v10H9l-4 3v-13Zm4 4h6m-6 3h4" />
        </svg>
        <div className="text-[13px]">暂无智能回复配置</div>
      </div>
    </div>
  );

  return (
    <div className={`relative flex-1 min-w-0 min-h-0 h-full overflow-hidden ${mobile ? "w-full" : "flex"}`}>
      {mobile ? (draft ? editorPane : listPane) : (
        <>
          <div className={`w-[300px] shrink-0 border-r ${dark ? "border-[#2a2a2a]" : "border-[#d7d7d7]"}`}>{listPane}</div>
          <div className="flex-1 min-w-0">{editorPane}</div>
        </>
      )}
      {pickerOpen && (
        <GroupPicker
          dark={dark}
          query={pickerQuery}
          onQueryChange={setPickerQuery}
          targets={availablePickerTargets}
          onSelect={openTarget}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}

function TargetAvatar({ target, size }: { target: SmartReplyTarget; size: number }) {
  const [failedSrc, setFailedSrc] = useState("");
  const avatar = target.avatar || "";
  return avatar && failedSrc !== avatar ? (
    <img src={avatar} alt="" onError={() => setFailedSrc(avatar)} className="rounded-[5px] object-cover shrink-0" style={{ width: size, height: size }} />
  ) : (
    <div className="rounded-[5px] bg-[#576b95] text-white flex items-center justify-center shrink-0" style={{ width: size, height: size, fontSize: Math.max(13, size * 0.38) }}>
      {(target.name || target.wxid || "?")[0]}
    </div>
  );
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative w-[42px] h-[24px] rounded-full transition-colors shrink-0 ${checked ? "bg-[#07c160]" : "bg-[#777]"}`}
    >
      <span className={`absolute left-[3px] top-[3px] w-[18px] h-[18px] rounded-full bg-white shadow transition-transform ${checked ? "translate-x-[18px]" : "translate-x-0"}`} />
    </button>
  );
}

function GroupPicker({
  dark,
  query,
  onQueryChange,
  targets,
  onSelect,
  onClose,
}: {
  dark: boolean;
  query: string;
  onQueryChange: (value: string) => void;
  targets: SmartReplyTarget[];
  onSelect: (target: SmartReplyTarget) => void;
  onClose: () => void;
}) {
  return (
    <div className="absolute inset-0 z-50 bg-black/55 flex items-center justify-center p-[16px]" onMouseDown={onClose}>
      <div className={`w-[420px] max-w-full h-[540px] max-h-[82vh] rounded-[7px] border shadow-2xl flex flex-col ${dark ? "bg-[#202020] border-[#414141] text-[#eee]" : "bg-white border-[#d0d0d0] text-[#111]"}`} onMouseDown={(event) => event.stopPropagation()}>
        <div className={`h-[56px] px-[16px] border-b flex items-center ${dark ? "border-[#333]" : "border-[#e5e5e5]"}`}>
          <div className="font-medium flex-1">选择群聊</div>
          <button type="button" title="关闭" onClick={onClose} className={`w-[32px] h-[32px] flex items-center justify-center ${dark ? "text-[#999]" : "text-[#666]"}`}>
            <svg className="w-[19px] h-[19px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24"><path strokeLinecap="round" d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <div className="p-[12px]">
          <input value={query} onChange={(event) => onQueryChange(event.target.value)} autoFocus placeholder="搜索群聊" className={`w-full h-[36px] rounded-[5px] px-[10px] outline-none border focus:border-[#07c160] ${dark ? "bg-[#151515] border-[#383838]" : "bg-[#fafafa] border-[#ddd]"}`} />
        </div>
        <div className="pane-scroll flex-1 min-h-0 overflow-y-auto">
          {targets.length === 0 ? (
            <div className={`py-[50px] text-center text-[13px] ${dark ? "text-[#666]" : "text-[#999]"}`}>没有可选群聊</div>
          ) : targets.map((target) => (
            <button key={target.wxid} type="button" onClick={() => onSelect(target)} className={`w-full h-[58px] px-[14px] flex items-center gap-[11px] text-left ${dark ? "hover:bg-[#2b2b2b]" : "hover:bg-[#f1f1f1]"}`}>
              <TargetAvatar target={target} size={38} />
              <div className="min-w-0 flex-1">
                <div className="text-[14px] truncate">{target.name || target.wxid}</div>
                <div className={`text-[10px] truncate mt-[2px] ${dark ? "text-[#666]" : "text-[#aaa]"}`}>{target.wxid}</div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
