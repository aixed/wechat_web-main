import { useCallback, useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from "react";
import { analyzeAiMessage, deleteSmartReply, getAiSettings, getGroupMemberDetails, getSmartReplies, saveAiSettings, saveSmartReply, validateAiSettings } from "../api";
import type { AiAnalysisResult, AiProfile, AiSettings, SmartReplyAiOutputMode, SmartReplyAiTask, SmartReplyConfig, SmartReplyMessageType, SmartReplyRule, SmartReplyTarget } from "../types";
import { DEFAULT_AVATAR_URL } from "../avatar";

interface GroupMember {
  wxid: string;
  name: string;
  avatar?: string;
}

interface SmartReplyManagerProps {
  theme: "dark" | "light";
  initialTarget?: SmartReplyTarget | null;
  availableTargets: SmartReplyTarget[];
  selfWxid?: string;
  mobile?: boolean;
}

type TextReplyMode = "rules" | "ai";

interface SmartReplyViewPreference {
  message_type?: SmartReplyMessageType;
  text_reply_mode?: TextReplyMode;
}

const SMART_REPLY_VIEW_PREFERENCES_KEY = "wechat-smart-reply-view-preferences-v1";

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

function readViewPreference(chatId: string): SmartReplyViewPreference {
  if (typeof window === "undefined") return {};
  try {
    const stored = JSON.parse(window.localStorage.getItem(SMART_REPLY_VIEW_PREFERENCES_KEY) || "{}");
    const preference = stored?.[chatId];
    return preference && typeof preference === "object" ? preference : {};
  } catch {
    return {};
  }
}

function writeViewPreference(chatId: string, patch: SmartReplyViewPreference) {
  if (typeof window === "undefined" || !chatId) return;
  try {
    const stored = JSON.parse(window.localStorage.getItem(SMART_REPLY_VIEW_PREFERENCES_KEY) || "{}");
    const preferences = stored && typeof stored === "object" ? stored : {};
    preferences[chatId] = { ...(preferences[chatId] || {}), ...patch };
    window.localStorage.setItem(SMART_REPLY_VIEW_PREFERENCES_KEY, JSON.stringify(preferences));
  } catch {
    // Browser storage can be unavailable in private or restricted contexts.
  }
}

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

function makeAiSkill(): SmartReplyAiTask {
  const suffix = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  const id = `ai_skill_${suffix}`;
  return {
    id,
    name: "未命名 Skill",
    enabled: true,
    skill_type: "custom",
    skill_id: id,
    instruction: "",
    confidence: 85,
    output_mode: "result",
    reply_template: "{{result}}",
    preserve_formatting: true,
    send_items_separately: false,
    max_parallel: 3,
  };
}

function makeDraft(target: SmartReplyTarget): SmartReplyConfig {
  const isGroup = target.wxid.endsWith("@chatroom");
  return {
    chat_id: target.wxid,
    chat_name: target.name || target.wxid,
    avatar: target.avatar || "",
    enabled: true,
    mention_only: false,
    message_types: ["text"],
    target_senders: isGroup ? [] : [target.wxid],
    rules: [makeRule()],
    ai_tasks: [],
    reply_count: 0,
    last_triggered_at: 0,
  };
}

function cloneConfig(config: SmartReplyConfig): SmartReplyConfig {
  const isGroup = config.chat_id.endsWith("@chatroom");
  return {
    ...config,
    mention_only: isGroup && Boolean(config.mention_only),
    message_types: config.message_types?.length ? [...config.message_types] : ["text"],
    target_senders: isGroup ? [...(config.target_senders || [])] : [config.chat_id],
    rules: (config.rules || []).map((rule) => ({
      ...rule,
      use_regex: Boolean(rule.use_regex),
      reply_with_matched_line: Boolean(rule.reply_with_matched_line),
    })),
    ai_tasks: (config.ai_tasks || []).map((task) => ({
      ...task,
      enabled: Boolean(task.enabled),
      skill_type: "custom",
      skill_id: task.skill_id || task.id,
      preserve_formatting: Boolean(task.preserve_formatting),
      send_items_separately: Boolean(task.send_items_separately),
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
  selfWxid = "",
  mobile = false,
}: SmartReplyManagerProps) {
  const dark = theme === "dark";
  const normalizedSelfWxid = selfWxid.trim();
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
  const [textReplyMode, setTextReplyMode] = useState<TextReplyMode>("rules");
  const [aiSettings, setAiSettings] = useState<AiSettings | null>(null);
  const [aiSettingsOpen, setAiSettingsOpen] = useState(false);
  const [aiTestTaskId, setAiTestTaskId] = useState("");
  const [aiTestMessage, setAiTestMessage] = useState("");
  const [aiTestResult, setAiTestResult] = useState<AiAnalysisResult | null>(null);
  const [aiTesting, setAiTesting] = useState(false);
  const [aiTestExpanded, setAiTestExpanded] = useState(false);
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

  useEffect(() => {
    if (!aiTestExpanded) return;
    const collapseTestOnOutsideClick = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      if (!event.target.closest("[data-ai-test-panel]")) {
        setAiTestExpanded(false);
      }
    };
    document.addEventListener("click", collapseTestOnOutsideClick);
    return () => document.removeEventListener("click", collapseTestOnOutsideClick);
  }, [aiTestExpanded]);

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

  const openAiSettings = async () => {
    setError("");
    try {
      const data = await getAiSettings(true);
      setAiSettings(data as AiSettings);
    } catch {
      setError("AI 配置读取失败，请检查 config.yaml 格式");
    } finally {
      setAiSettingsOpen(true);
    }
  };

  const openConfig = useCallback((config: SmartReplyConfig) => {
    const cloned = cloneConfig(config);
    const preference = readViewPreference(config.chat_id);
    const preferredMessageType = MESSAGE_TYPE_OPTIONS.some(
      (option) => option.value === preference.message_type,
    )
      ? preference.message_type as SmartReplyMessageType
      : cloned.message_types[0] || "text";
    const preferredReplyMode = preference.text_reply_mode === "rules" || preference.text_reply_mode === "ai"
      ? preference.text_reply_mode
      : cloned.ai_tasks.length > 0 ? "ai" : "rules";
    setDraft(cloned);
    setActiveMessageType(preferredMessageType);
    setTextReplyMode(preferredReplyMode);
    setAiTestTaskId(cloned.ai_tasks[0]?.id || "");
    setAiTestResult(null);
    setAiTestExpanded(false);
    setExpandedRuleId(null);
    setError("");
    setNotice("");
    if (config.chat_id.endsWith("@chatroom")) {
      loadMembers(config.chat_id);
    } else {
      setMembers([]);
      setMemberQuery("");
      setMembersLoading(false);
    }
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

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await getAiSettings();
        if (!cancelled) setAiSettings(data as AiSettings);
      } catch {
        if (!cancelled) setAiSettings(null);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async () => {
    if (!draft || saving) return;
    const normalizedRules = draft.rules.map((rule) => ({
      ...rule,
      keyword: rule.keyword.trim(),
      reply: rule.reply.trim(),
    }));
    const rules = normalizedRules.filter((rule) => rule.keyword || rule.reply);
    const aiTasks = draft.ai_tasks.map((task) => ({
      ...task,
      name: task.name.trim(),
      skill_type: "custom" as const,
      skill_id: task.skill_id.trim() || task.id,
      instruction: task.instruction.trim(),
      reply_template: task.reply_template.trim(),
    }));
    if (draft.target_senders.length === 0) {
      setError("请选择至少一位目标发送人");
      return;
    }
    if (draft.message_types.length === 0) {
      setError("请至少选择一种消息类型");
      return;
    }
    if (rules.some((rule) => !rule.keyword || (!rule.reply_with_matched_line && !rule.reply))) {
      setError("请完整填写关键词和回复内容");
      return;
    }
    if (aiTasks.some((task) => !task.name || !task.instruction)) {
      setError("请完整填写 Skill 名称和任务指令");
      return;
    }
    if (rules.length === 0 && aiTasks.length === 0) {
      setError("请至少配置一条关键词规则或一个 AI 任务");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const data = await saveSmartReply(draft.chat_id, {
        chat_name: draft.chat_name,
        avatar: draft.avatar,
        enabled: draft.enabled,
        mention_only: draft.mention_only,
        message_types: draft.message_types,
        target_senders: draft.target_senders,
        rules,
        ai_tasks: aiTasks,
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

  const currentAiTasks = draft?.ai_tasks || [];

  const updateAiTask = (id: string, patch: Partial<SmartReplyAiTask>) => {
    setDraft((prev) => prev ? {
      ...prev,
      ai_tasks: prev.ai_tasks.map((task) => task.id === id ? { ...task, ...patch } : task),
    } : prev);
    setNotice("");
    setAiTestResult(null);
  };

  const addAiTask = () => {
    const task = makeAiSkill();
    setDraft((prev) => prev ? { ...prev, ai_tasks: [...prev.ai_tasks, task] } : prev);
    setAiTestTaskId(task.id);
    setNotice("");
    setAiTestResult(null);
  };

  const removeAiTask = (id: string) => {
    setDraft((prev) => {
      if (!prev) return prev;
      const aiTasks = prev.ai_tasks.filter((task) => task.id !== id);
      setAiTestTaskId((current) => current === id ? (aiTasks[0]?.id || "") : current);
      return { ...prev, ai_tasks: aiTasks };
    });
    setNotice("");
    setAiTestResult(null);
  };

  const testAiTask = async () => {
    const task = currentAiTasks.find((item) => item.id === aiTestTaskId) || currentAiTasks[0];
    if (!task || aiTesting) return;
    if (!task.name.trim() || !task.instruction.trim()) {
      setError("请先填写 Skill 名称和任务指令");
      return;
    }
    if (!aiSettings?.configured) {
      setError("请先完成全局 AI 服务配置");
      void openAiSettings();
      return;
    }
    if (!aiTestMessage.trim()) {
      setError("请输入测试消息");
      return;
    }
    setAiTesting(true);
    setAiTestResult(null);
    setError("");
    try {
      const data = await analyzeAiMessage(aiTestMessage, task);
      if (!data?.ok || !data?.result) {
        setError(errorText(data?.detail || data?.error));
        return;
      }
      setAiTestResult(data.result as AiAnalysisResult);
    } catch {
      setError("AI 测试失败，请检查服务地址和网络连接");
    } finally {
      setAiTesting(false);
    }
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
    else {
      if (normalizedSelfWxid && wxid === normalizedSelfWxid) {
        const confirmed = window.confirm(
          `“${wxid}”是当前登录账号。自己的消息会被系统过滤，通常不需要加入目标发送人。仍要选中吗？`,
        );
        if (!confirmed) return;
      }
      selected.add(wxid);
    }
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

  const selectableMemberWxids = useMemo(
    () => members
      .filter((member) => !normalizedSelfWxid || member.wxid !== normalizedSelfWxid)
      .map((member) => member.wxid),
    [members, normalizedSelfWxid],
  );
  const allSelectableMembersSelected = selectableMemberWxids.length > 0
    && selectableMemberWxids.every((wxid) => draft?.target_senders.includes(wxid));
  const selfSelected = Boolean(
    normalizedSelfWxid && draft?.target_senders.includes(normalizedSelfWxid),
  );

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

  const aiTestPanel = textReplyMode === "ai" && currentAiTasks.length > 0 ? (
    <div data-ai-test-panel className={`mt-[24px] border-y ${dark ? "border-[#303030]" : "border-[#ddd]"}`}>
      <button
        type="button"
        aria-expanded={aiTestExpanded}
        onClick={() => setAiTestExpanded((expanded) => !expanded)}
        className={`w-full min-h-[58px] py-[10px] flex items-center gap-[12px] text-left ${dark ? "hover:bg-[#171717]" : "hover:bg-[#f7f7f7]"}`}
      >
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-medium">Skill 测试</h3>
          <div className={`mt-[3px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>选择自定义 Skill 测试，不发送微信消息</div>
        </div>
        <svg className={`w-[18px] h-[18px] shrink-0 transition-transform ${aiTestExpanded ? "rotate-180" : ""} ${dark ? "text-[#777]" : "text-[#888]"}`} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {aiTestExpanded && (
        <div className={`pb-[18px] pt-[14px] border-t ${dark ? "border-[#292929]" : "border-[#e5e5e5]"}`}>
          <div className="flex flex-wrap items-center gap-[10px]">
            <select
              aria-label="测试使用的 Skill"
              value={aiTestTaskId || currentAiTasks[0]?.id || ""}
              onChange={(event) => {
                setAiTestTaskId(event.target.value);
                setAiTestResult(null);
              }}
              className={`min-w-[220px] flex-1 h-[38px] rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111]" : "border-[#d8d8d8] bg-[#fafafa]"}`}
            >
              {currentAiTasks.map((task) => <option key={task.id} value={task.id}>{task.name}</option>)}
            </select>
            {!aiSettings?.configured && (
              <button type="button" onClick={() => void openAiSettings()} className="h-[38px] px-[12px] rounded-[5px] bg-[#07c160] text-white text-[13px]">
                配置全局 AI
              </button>
            )}
          </div>
          <textarea
            value={aiTestMessage}
            onChange={(event) => {
              setAiTestMessage(event.target.value);
              setAiTestResult(null);
            }}
            rows={7}
            maxLength={20000}
            placeholder="粘贴一条需要交给该 Skill 处理的消息"
            className={`mt-[12px] block w-full min-h-[150px] resize-y rounded-[5px] border px-[10px] py-[8px] font-mono text-[13px] leading-[20px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
          />
          <div className="mt-[10px] flex justify-end">
            <button type="button" disabled={aiTesting} onClick={() => void testAiTask()} className="h-[34px] px-[14px] rounded-[5px] bg-[#07c160] text-white text-[13px] disabled:bg-[#315541]">
              {aiTesting ? "处理中" : "开始测试"}
            </button>
          </div>
          {aiTestResult && (
            <div className={`mt-[14px] rounded-[6px] border px-[14px] py-[12px] ${dark ? "border-[#303030] bg-[#151515]" : "border-[#dcdcdc] bg-white"}`}>
              <div className="flex flex-wrap items-center gap-x-[16px] gap-y-[5px] text-[12px]">
                <span className={aiTestResult.matched ? "text-[#07c160]" : (dark ? "text-[#999]" : "text-[#666]")}>{aiTestResult.matched ? "已命中" : "未命中"}</span>
                <span className={dark ? "text-[#888]" : "text-[#777]"}>置信度 {aiTestResult.confidence}%</span>
              </div>
              <div className={`mt-[9px] whitespace-pre-wrap break-words text-[14px] leading-[22px] ${dark ? "text-[#ddd]" : "text-[#222]"}`}>
                {aiTestResult.reply || aiTestResult.result || "AI 未生成处理结果"}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  ) : null;

  const listPane = (
    <div className={`h-full flex flex-col ${dark ? "bg-[#191919]" : "bg-[#e9e8e8]"}`}>
      <div className={`h-[62px] px-[14px] flex items-center border-b ${dark ? "border-[#2a2a2a]" : "border-[#d7d7d7]"}`}>
        {mobile ? (
          <div className={`w-[38px] ${dark ? "text-[#aaa]" : "text-[#555]"}`} />
        ) : null}
        <h1 className={`text-[17px] font-medium flex-1 ${mobile ? "text-center" : ""}`}>智能回复</h1>
        <div className="flex items-center gap-[2px]">
          <button
            type="button"
            title={aiSettings?.configured ? "全局 AI 设置" : "配置全局 AI 服务"}
            aria-label="全局 AI 设置"
            onClick={() => void openAiSettings()}
            className={`relative w-[34px] h-[34px] flex items-center justify-center rounded-[5px] ${dark ? "text-[#bbb] hover:bg-[#292929]" : "text-[#444] hover:bg-[#d8d8d8]"}`}
          >
            <svg className="w-[19px] h-[19px]" fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21H9.6v-.1A1.7 1.7 0 0 0 8.5 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3V9.6h.1A1.7 1.7 0 0 0 4.6 8.5a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.1A1.7 1.7 0 0 0 15.5 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.17.37.38.7.6 1 .28.3.66.45 1.1.45h.1v4h-.1c-.44 0-.82.15-1.1.45-.22.3-.43.63-.6 1Z" />
            </svg>
            {!aiSettings?.configured && <span className="absolute right-[5px] top-[5px] w-[7px] h-[7px] rounded-full bg-[#e0a63b] ring-2 ring-current" />}
          </button>
          <button
            type="button"
            title="添加智能回复"
            onClick={() => setPickerOpen(true)}
            className={`w-[34px] h-[34px] flex items-center justify-center rounded-[5px] ${dark ? "text-[#bbb] hover:bg-[#292929]" : "text-[#444] hover:bg-[#d8d8d8]"}`}
          >
            <svg className="w-[20px] h-[20px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
              <path strokeLinecap="round" d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </div>
      </div>
      <div className="p-[10px]">
        <div className={`h-[34px] rounded-[6px] flex items-center px-[10px] ${dark ? "bg-[#262626]" : "bg-[#dcdcdc]"}`}>
          <svg className={`w-[15px] h-[15px] shrink-0 ${dark ? "text-[#666]" : "text-[#888]"}`} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
            <path strokeLinecap="round" d="m21 21-5-5m2-6a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" />
          </svg>
          <input
            value={listQuery}
            onChange={(event) => setListQuery(event.target.value)}
            placeholder="搜索聊天"
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
                  {config.target_senders.length} 人 · {config.rules.length} 规则 · {config.ai_tasks?.length || 0} AI
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
          className="h-[34px] px-[15px] rounded-[5px] text-white text-[13px] bg-[#07c160] disabled:bg-[#315541]"
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
            <div className="flex items-center justify-between gap-[18px]">
              <div>
                <h2 className="text-[15px] font-medium">消息类别</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>已启用 {draft.message_types.length} 类</div>
              </div>
              {draft.chat_id.endsWith("@chatroom") && (
                <div
                  className="flex items-center gap-[8px] text-[13px]"
                  title="开启后只处理目标发送人 @ 当前账号的消息"
                >
                  <span>只处理 @本人 消息</span>
                  <CompactToggle
                    checked={draft.mention_only}
                    onChange={(mention_only) => updateDraft({ mention_only })}
                    label="只处理 @本人 消息"
                  />
                </div>
              )}
            </div>
            <div role="tablist" className={`mt-[14px] flex flex-wrap items-center gap-x-[12px] gap-y-[4px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
              {MESSAGE_TYPE_OPTIONS.map((option) => {
                const enabled = draft.message_types.includes(option.value);
                const active = activeMessageType === option.value;
                return (
                <div
                  key={option.value}
                  className={`h-[38px] flex items-center gap-[4px] border-b-2 text-[13px] ${
                    active
                      ? "border-[#07c160] text-[#07c160]"
                      : `border-transparent ${dark ? "text-[#aaa] hover:text-white" : "text-[#666] hover:text-black"}`
                  }`}
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => {
                      setActiveMessageType(option.value);
                      writeViewPreference(draft.chat_id, { message_type: option.value });
                      setExpandedRuleId(null);
                    }}
                    className="h-full"
                  >
                    {option.label}
                  </button>
                  <span className={`w-[5px] h-[5px] rounded-full ${enabled ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#bbb]")}`} />
                  <CompactToggle
                    checked={enabled}
                    onChange={() => toggleMessageType(option.value)}
                    label={`${enabled ? "关闭" : "启用"}${option.label}`}
                  />
                </div>
                );
              })}
            </div>
          </section>

          {activeMessageType === "text" ? (
            <>
          <section className="pt-[16px]">
            <div role="tablist" aria-label="文本回复方式" className={`flex items-center gap-x-[20px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
              <button
                type="button"
                role="tab"
                aria-selected={textReplyMode === "rules"}
                onClick={() => {
                  setTextReplyMode("rules");
                  writeViewPreference(draft.chat_id, { text_reply_mode: "rules" });
                }}
                className={`h-[38px] flex items-center gap-[7px] border-b-2 text-[13px] ${
                  textReplyMode === "rules"
                    ? "border-[#07c160] text-[#07c160]"
                    : `border-transparent ${dark ? "text-[#aaa] hover:text-white" : "text-[#666] hover:text-black"}`
                }`}
              >
                <span>规则回复</span>
                <span className={`w-[6px] h-[6px] rounded-full ${textReplyMode === "rules" ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#bbb]")}`} />
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={textReplyMode === "ai"}
                onClick={() => {
                  setTextReplyMode("ai");
                  writeViewPreference(draft.chat_id, { text_reply_mode: "ai" });
                }}
                className={`h-[38px] flex items-center gap-[7px] border-b-2 text-[13px] ${
                  textReplyMode === "ai"
                    ? "border-[#07c160] text-[#07c160]"
                    : `border-transparent ${dark ? "text-[#aaa] hover:text-white" : "text-[#666] hover:text-black"}`
                }`}
              >
                <span>AI 智能回复</span>
                <span className={`w-[6px] h-[6px] rounded-full ${textReplyMode === "ai" ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#bbb]")}`} />
              </button>
            </div>
          </section>

          {draft.chat_id.endsWith("@chatroom") && (
          <section className={`py-[24px] border-b ${dark ? "border-[#292929]" : "border-[#ddd]"}`}>
            <div className="flex items-center justify-between gap-[12px]">
              <div>
                <h2 className="text-[15px] font-medium">监听目标发送人的消息</h2>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>已选择 {draft.target_senders.length} 人</div>
                {selfSelected && (
                  <div role="alert" className={`mt-[5px] text-[12px] ${dark ? "text-[#d4a657]" : "text-[#a76b00]"}`}>
                    当前登录账号已被选中，建议取消以避免误配置。
                  </div>
                )}
              </div>
              {selectableMemberWxids.length > 0 && (
                <button
                  type="button"
                  onClick={() => updateDraft({
                    target_senders: allSelectableMembersSelected ? [] : selectableMemberWxids,
                  })}
                  className={`text-[13px] ${dark ? "text-[#9a9a9a] hover:text-white" : "text-[#666] hover:text-black"}`}
                >
                  {allSelectableMembersSelected ? "取消全选" : "全选"}
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
                  const isSelf = Boolean(normalizedSelfWxid && member.wxid === normalizedSelfWxid);
                  return (
                    <label
                      key={member.wxid}
                      title={isSelf ? "当前登录账号" : undefined}
                      className={`h-[52px] flex items-center gap-[10px] border-b cursor-pointer ${
                        dark ? "border-[#252525]" : "border-[#e5e5e5]"
                      } ${isSelf ? (dark ? "bg-[#151515] text-[#686868]" : "bg-[#f1f1f1] text-[#999]") : ""}`}
                    >
                      <input type="checkbox" checked={checked} onChange={() => toggleSender(member.wxid)} className={`w-[16px] h-[16px] accent-[#07c160] ${isSelf ? "opacity-60" : ""}`} />
                      <div className={isSelf ? "opacity-55 grayscale" : ""}>
                        <TargetAvatar target={member} size={34} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-[7px] min-w-0">
                          <span className="text-[13px] truncate">{member.name}</span>
                          {isSelf && (
                            <span className={`shrink-0 text-[10px] ${dark ? "text-[#626262]" : "text-[#999]"}`}>自己</span>
                          )}
                        </div>
                        <div className={`text-[10px] truncate mt-[1px] ${dark ? "text-[#606060]" : "text-[#aaa]"}`}>{member.wxid}</div>
                      </div>
                    </label>
                  );
                })}
              </div>
            )}
          </section>
          )}

          {aiTestPanel}

          {textReplyMode === "rules" ? (
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
          ) : (
          <section className="py-[24px]">
            <div className="flex flex-wrap items-start justify-between gap-[12px]">
              <div>
                <div className="flex items-center gap-[8px]">
                  <h2 className="text-[15px] font-medium">自定义 Skill</h2>
                  <span className={`text-[10px] px-[6px] py-[2px] rounded-[3px] ${
                    aiSettings?.configured
                      ? "bg-[#173d28] text-[#55d88a]"
                      : (dark ? "bg-[#292929] text-[#999]" : "bg-[#e4e4e4] text-[#777]")
                  }`}>{aiSettings?.configured ? aiSettings.model : "AI 服务待配置"}</span>
                </div>
                <div className={`mt-[4px] text-[12px] ${dark ? "text-[#777]" : "text-[#888]"}`}>{currentAiTasks.length} 个 Skill</div>
              </div>
              <div className="flex items-center gap-[8px]">
                <button
                  type="button"
                  onClick={addAiTask}
                  className={`h-[32px] px-[11px] rounded-[5px] border text-[13px] ${dark ? "border-[#3b3b3b] hover:bg-[#222]" : "border-[#d2d2d2] bg-white hover:bg-[#f0f0f0]"}`}
                >
                  新建 Skill
                </button>
              </div>
            </div>

            {currentAiTasks.length === 0 ? (
              <div className={`mt-[18px] py-[42px] border text-center text-[13px] rounded-[6px] ${dark ? "border-[#303030] text-[#666]" : "border-[#ddd] text-[#999]"}`}>
                暂无自定义 Skill
              </div>
            ) : (
              <div className="mt-[14px] space-y-[12px]">
                {currentAiTasks.map((task, index) => (
                  <div key={task.id} className={`rounded-[6px] border p-[14px] ${dark ? "border-[#303030] bg-[#181818]" : "border-[#dcdcdc] bg-white"}`}>
                    <div className="flex items-center gap-[10px]">
                      <span className={`text-[12px] w-[22px] shrink-0 ${dark ? "text-[#666]" : "text-[#999]"}`}>{index + 1}</span>
                      <Toggle checked={task.enabled} onChange={(enabled) => updateAiTask(task.id, { enabled })} />
                      <input
                        value={task.name}
                        onChange={(event) => updateAiTask(task.id, { name: event.target.value })}
                        maxLength={80}
                        aria-label={`Skill ${index + 1} 名称`}
                        className={`h-[36px] min-w-0 flex-1 rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111]" : "border-[#d8d8d8] bg-[#fafafa]"}`}
                      />
                      <button
                        type="button"
                        title="删除 Skill"
                        aria-label={`删除 Skill ${index + 1}`}
                        onClick={() => removeAiTask(task.id)}
                        className={`w-[34px] h-[34px] flex items-center justify-center ${dark ? "text-[#888] hover:text-[#e57373]" : "text-[#777] hover:text-[#c33]"}`}
                      >
                        <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5" />
                        </svg>
                      </button>
                    </div>

                    <div className="mt-[16px]">
                      <label className={`block text-[12px] mb-[6px] ${dark ? "text-[#888]" : "text-[#666]"}`}>Skill 指令</label>
                      <textarea
                        value={task.instruction}
                        onChange={(event) => updateAiTask(task.id, { instruction: event.target.value })}
                        maxLength={4000}
                        rows={4}
                        placeholder="描述需要识别、提取或判断的内容"
                        className={`block w-full h-[94px] resize-none overflow-y-auto rounded-[5px] border px-[10px] py-[8px] leading-[20px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                      />
                    </div>

                    <div className={`mt-[16px] pt-[16px] border-t grid grid-cols-1 lg:grid-cols-2 gap-x-[20px] gap-y-[16px] ${dark ? "border-[#2d2d2d]" : "border-[#e4e4e4]"}`}>
                      <div>
                        <div className="flex items-center justify-between gap-[10px]">
                          <label className={`text-[12px] ${dark ? "text-[#888]" : "text-[#666]"}`}>最低置信度</label>
                          <span className={`text-[12px] tabular-nums ${dark ? "text-[#aaa]" : "text-[#555]"}`}>{task.confidence}%</span>
                        </div>
                        <input
                          type="range"
                          min={50}
                          max={100}
                          step={5}
                          value={task.confidence}
                          onChange={(event) => updateAiTask(task.id, { confidence: Number(event.target.value) })}
                          className="mt-[11px] w-full accent-[#07c160]"
                        />
                      </div>
                      <div>
                        <label className={`block text-[12px] mb-[6px] ${dark ? "text-[#888]" : "text-[#666]"}`}>回复方式</label>
                        <select
                          value={task.output_mode}
                          onChange={(event) => updateAiTask(task.id, { output_mode: event.target.value as SmartReplyAiOutputMode })}
                          className={`w-full h-[38px] rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111]" : "border-[#d8d8d8] bg-[#fafafa]"}`}
                        >
                          <option value="result">直接回复 Skill 结果</option>
                          <option value="template">使用模板回复</option>
                          <option value="silent">仅识别，不自动回复</option>
                        </select>
                      </div>
                    </div>

                    {task.output_mode === "template" && (
                      <div className="mt-[16px]">
                        <label className={`block text-[12px] mb-[6px] ${dark ? "text-[#888]" : "text-[#666]"}`}>回复模板</label>
                        <textarea
                          value={task.reply_template}
                          onChange={(event) => updateAiTask(task.id, { reply_template: event.target.value })}
                          maxLength={4000}
                          rows={3}
                          placeholder="使用 {{result}} 引用 Skill 处理结果"
                          className={`block w-full h-[74px] resize-none rounded-[5px] border px-[10px] py-[8px] leading-[20px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                        />
                      </div>
                    )}

                    <div className={`mt-[16px] min-h-[42px] flex flex-wrap items-center gap-x-[22px] gap-y-[10px] rounded-[5px] border px-[10px] py-[9px] text-[13px] ${dark ? "border-[#393939] bg-[#111] text-[#ccc]" : "border-[#d8d8d8] bg-[#fafafa] text-[#333]"}`}>
                      <label className="flex items-center gap-[7px] cursor-pointer select-none">
                        <input type="checkbox" checked={task.preserve_formatting} onChange={(event) => updateAiTask(task.id, { preserve_formatting: event.target.checked })} className="w-[16px] h-[16px] accent-[#07c160]" />
                        <span>保留结果格式</span>
                      </label>
                      <label className="flex items-center gap-[7px] cursor-pointer select-none">
                        <input type="checkbox" checked={task.send_items_separately} onChange={(event) => updateAiTask(task.id, { send_items_separately: event.target.checked })} className="w-[16px] h-[16px] accent-[#07c160]" />
                        <span>逐项发送</span>
                      </label>
                      {task.send_items_separately && (
                        <label className="flex items-center gap-[7px]">
                          <span>并发数</span>
                          <input
                            type="number"
                            min={1}
                            max={10}
                            value={task.max_parallel}
                            onChange={(event) => updateAiTask(task.id, { max_parallel: Math.max(1, Math.min(10, Number(event.target.value) || 1)) })}
                            className={`w-[58px] h-[28px] rounded-[4px] border px-[7px] outline-none focus:border-[#07c160] ${dark ? "border-[#414141] bg-[#191919]" : "border-[#d2d2d2] bg-white"}`}
                          />
                        </label>
                      )}
                    </div>

                  </div>
                ))}
              </div>
            )}
          </section>
          )}
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
      {aiSettingsOpen && (
        <AiSettingsDialog
          dark={dark}
          settings={aiSettings}
          onClose={() => setAiSettingsOpen(false)}
          onSaved={(settings) => {
            setAiSettings(settings);
            setAiSettingsOpen(false);
            setError("");
            setNotice("AI 服务配置已保存");
            window.setTimeout(() => setNotice(""), 1800);
          }}
        />
      )}
    </div>
  );
}

function TargetAvatar({ target, size }: { target: SmartReplyTarget; size: number }) {
  const [failedSrc, setFailedSrc] = useState("");
  const avatar = target.avatar || "";
  return (
    <img
      src={avatar && failedSrc !== avatar ? avatar : DEFAULT_AVATAR_URL}
      alt={target.name || target.wxid}
      onError={() => setFailedSrc(avatar)}
      className="rounded-[5px] object-cover shrink-0"
      style={{ width: size, height: size }}
    />
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

function CompactToggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-label={label}
      aria-checked={checked}
      title={label}
      onClick={() => onChange(!checked)}
      className={`relative w-[26px] h-[15px] rounded-full transition-colors shrink-0 ${checked ? "bg-[#07c160]" : "bg-[#777]"}`}
    >
      <span className={`absolute left-[2px] top-[2px] w-[11px] h-[11px] rounded-full bg-white shadow transition-transform ${checked ? "translate-x-[11px]" : "translate-x-0"}`} />
    </button>
  );
}

function makeAiProfileDraft(index = 0): AiProfile {
  const suffix = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  return {
    id: `ai_profile_${suffix}`,
    name: `AI 配置 ${index + 1}`,
    configured: false,
    api_key_configured: false,
    api_key: "",
    base_url: "",
    model: "",
  };
}

function normalizeAiProfiles(settings: AiSettings | null): AiProfile[] {
  const profiles = Array.isArray(settings?.profiles) ? settings.profiles : [];
  if (profiles.length > 0) {
    return profiles.map((profile, index) => ({
      id: profile.id || `ai_profile_${index + 1}`,
      name: profile.name || profile.model || `AI 配置 ${index + 1}`,
      configured: Boolean(profile.configured),
      api_key_configured: Boolean(profile.api_key_configured || profile.api_key),
      api_key: profile.api_key || "",
      base_url: profile.base_url || "",
      model: profile.model || "",
    }));
  }
  if (settings?.base_url || settings?.model || settings?.api_key_configured || settings?.api_key) {
    return [{
      id: settings.active_profile_id || "default",
      name: settings.model || "默认配置",
      configured: Boolean(settings.configured),
      api_key_configured: Boolean(settings.api_key_configured || settings.api_key),
      api_key: settings.api_key || "",
      base_url: settings.base_url || "",
      model: settings.model || "",
    }];
  }
  return [makeAiProfileDraft(0)];
}

function AiSettingsDialog({
  dark,
  settings,
  onClose,
  onSaved,
}: {
  dark: boolean;
  settings: AiSettings | null;
  onClose: () => void;
  onSaved: (settings: AiSettings) => void;
}) {
  const initialProfiles = useMemo(() => normalizeAiProfiles(settings), [settings]);
  const [profiles, setProfiles] = useState<AiProfile[]>(initialProfiles);
  const [activeProfileId, setActiveProfileId] = useState(settings?.active_profile_id || initialProfiles[0]?.id || "");
  const [expandedProfileId, setExpandedProfileId] = useState(() => {
    const incomplete = initialProfiles.find((profile) =>
      !profile.base_url.trim() || !profile.model.trim() || (!profile.api_key_configured && !profile.api_key?.trim())
    );
    return incomplete?.id || "";
  });
  const [showApiKeyById, setShowApiKeyById] = useState<Record<string, boolean>>({});
  const [savingSettings, setSavingSettings] = useState(false);
  const [validatingProfileId, setValidatingProfileId] = useState("");
  const [settingsFeedback, setSettingsFeedback] = useState<{ kind: "success" | "error"; message: string; profileId?: string } | null>(null);

  const activeProfile = profiles.find((profile) => profile.id === activeProfileId) || profiles[0];

  const updateProfile = (id: string, patch: Partial<AiProfile>) => {
    setProfiles((prev) => prev.map((profile) => profile.id === id ? { ...profile, ...patch } : profile));
    setSettingsFeedback(null);
  };

  const profileIsComplete = (profile: AiProfile) =>
    Boolean(profile.base_url.trim() && profile.model.trim() && (profile.api_key?.trim() || profile.api_key_configured));

  const profilePayload = (profile: AiProfile) => ({
    active_profile_id: profile.id,
    profiles: [{
      id: profile.id,
      name: profile.name.trim(),
      base_url: profile.base_url.trim().replace(/\/+$/, ""),
      api_key: (profile.api_key || "").trim(),
      model: profile.model.trim(),
    }],
  });

  const settingsPayload = () => ({
    active_profile_id: activeProfileId,
    profiles: profiles.map((profile) => ({
      id: profile.id,
      name: profile.name.trim(),
      base_url: profile.base_url.trim().replace(/\/+$/, ""),
      api_key: (profile.api_key || "").trim(),
      model: profile.model.trim(),
    })),
  });

  const validateProfile = async (profile: AiProfile) => {
    if (savingSettings || validatingProfileId) return;
    if (!profileIsComplete(profile)) {
      setExpandedProfileId(profile.id);
      setSettingsFeedback({ kind: "error", profileId: profile.id, message: "请完整填写 API 地址、API Key 和模型" });
      return;
    }
    setValidatingProfileId(profile.id);
    setSettingsFeedback(null);
    try {
      const data = await validateAiSettings(profilePayload(profile));
      if (!data?.ok) {
        setSettingsFeedback({ kind: "error", profileId: profile.id, message: errorText(data?.detail || data?.error) });
        return;
      }
      setSettingsFeedback({ kind: "success", profileId: profile.id, message: `校验成功，模型 ${profile.model.trim()} 可以使用` });
    } catch {
      setSettingsFeedback({ kind: "error", profileId: profile.id, message: "校验失败，请检查地址、密钥和网络" });
    } finally {
      setValidatingProfileId("");
    }
  };

  const submit = async () => {
    if (savingSettings || validatingProfileId) return;
    if (profiles.length === 0) {
      setSettingsFeedback({ kind: "error", message: "请至少保留一个 AI 配置" });
      return;
    }
    const incomplete = profiles.find((profile) => !profileIsComplete(profile));
    if (incomplete) {
      setExpandedProfileId(incomplete.id);
      setSettingsFeedback({ kind: "error", profileId: incomplete.id, message: "请完整填写该配置后再保存" });
      return;
    }
    if (!activeProfile || !profileIsComplete(activeProfile)) {
      setSettingsFeedback({ kind: "error", message: "请选择一个可用的配置作为当前使用模型" });
      return;
    }
    setSavingSettings(true);
    setSettingsFeedback(null);
    try {
      const data = await saveAiSettings(settingsPayload());
      if (!data?.ok) {
        setSettingsFeedback({ kind: "error", message: errorText(data?.detail || data?.error) });
        return;
      }
      onSaved(data as AiSettings);
    } catch {
      setSettingsFeedback({ kind: "error", message: "保存失败，请检查 config.yaml 是否可写" });
    } finally {
      setSavingSettings(false);
    }
  };

  const addProfile = () => {
    const profile = makeAiProfileDraft(profiles.length);
    setProfiles((prev) => [...prev, profile]);
    setActiveProfileId((current) => current || profile.id);
    setExpandedProfileId(profile.id);
    setSettingsFeedback(null);
  };

  const removeProfile = (id: string) => {
    setProfiles((prev) => {
      if (prev.length <= 1) {
        const empty = makeAiProfileDraft(0);
        setActiveProfileId(empty.id);
        setExpandedProfileId(empty.id);
        return [empty];
      }
      const next = prev.filter((profile) => profile.id !== id);
      if (activeProfileId === id) setActiveProfileId(next[0]?.id || "");
      if (expandedProfileId === id) setExpandedProfileId("");
      return next;
    });
    setSettingsFeedback(null);
  };

  const collapseOnBlank = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest("[data-ai-profile-card]") && !event.target.closest("[data-ai-dialog-action]")) {
      setExpandedProfileId("");
    }
  };

  return (
    <div className="absolute inset-0 z-[60] bg-black/55 flex items-center justify-center p-[16px]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="ai-settings-title"
        className={`w-[900px] max-w-full max-h-[88vh] rounded-[7px] border shadow-2xl flex flex-col ${dark ? "bg-[#202020] border-[#414141] text-[#eee]" : "bg-white border-[#d0d0d0] text-[#111]"}`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className={`h-[58px] px-[18px] border-b flex items-center ${dark ? "border-[#333]" : "border-[#e5e5e5]"}`}>
          <div className="min-w-0 flex-1">
            <div id="ai-settings-title" className="font-medium">全局 AI 设置</div>
            <div className={`mt-[3px] text-[12px] truncate ${dark ? "text-[#777]" : "text-[#888]"}`}>
              当前使用：{activeProfile?.name || "未选择"}{activeProfile?.model ? ` · ${activeProfile.model}` : ""}
            </div>
          </div>
          <button type="button" title="关闭" onClick={onClose} className={`w-[32px] h-[32px] flex items-center justify-center ${dark ? "text-[#999]" : "text-[#666]"}`}>
            <svg className="w-[19px] h-[19px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24"><path strokeLinecap="round" d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>

        <div className="pane-scroll flex-1 min-h-0 overflow-y-auto p-[18px]" onMouseDown={collapseOnBlank}>
          <div className="flex items-center justify-between gap-[12px] mb-[12px]">
            <div className={`text-[12px] leading-[19px] ${dark ? "text-[#888]" : "text-[#777]"}`}>
              可以添加多个模型配置，绿色圆点表示当前智能回复实际使用的配置。点击卡片展开编辑，点击空白处收起。
            </div>
            <button
              type="button"
              data-ai-dialog-action
              onClick={addProfile}
              className={`h-[32px] px-[12px] rounded-[5px] border text-[13px] shrink-0 ${dark ? "border-[#3b3b3b] hover:bg-[#292929]" : "border-[#d2d2d2] bg-white hover:bg-[#f0f0f0]"}`}
            >
              新建配置
            </button>
          </div>

          <div className="space-y-[10px]">
            {profiles.map((profile, index) => {
              const expanded = expandedProfileId === profile.id;
              const active = activeProfileId === profile.id;
              const showApiKey = Boolean(showApiKeyById[profile.id]);
              const feedback = settingsFeedback?.profileId === profile.id ? settingsFeedback : null;
              return (
                <div
                  key={profile.id}
                  data-ai-profile-card
                  className={`rounded-[7px] border overflow-hidden ${active ? "border-[#07c160]" : (dark ? "border-[#333]" : "border-[#ddd]")} ${dark ? "bg-[#181818]" : "bg-white"}`}
                >
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedProfileId((current) => current === profile.id ? "" : profile.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setExpandedProfileId((current) => current === profile.id ? "" : profile.id);
                      }
                    }}
                    className={`w-full min-h-[62px] px-[14px] py-[10px] flex items-center gap-[12px] text-left cursor-pointer ${dark ? "hover:bg-[#202020]" : "hover:bg-[#f7f7f7]"}`}
                  >
                    <span className={`w-[10px] h-[10px] rounded-full shrink-0 ${active ? "bg-[#07c160]" : (dark ? "bg-[#555]" : "bg-[#c8c8c8]")}`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-[8px]">
                        <span className="font-medium truncate">{profile.name || `AI 配置 ${index + 1}`}</span>
                        {active && <span className="text-[10px] px-[6px] py-[2px] rounded-full bg-[#173d28] text-[#55d88a]">当前使用</span>}
                        {!profileIsComplete(profile) && <span className={`text-[10px] px-[6px] py-[2px] rounded-full ${dark ? "bg-[#33251b] text-[#e0a63b]" : "bg-[#fff6df] text-[#9a6a16]"}`}>待完善</span>}
                      </div>
                      <div className={`mt-[4px] text-[12px] truncate ${dark ? "text-[#777]" : "text-[#888]"}`}>
                        {profile.model || "未填写模型"} · {profile.base_url || "未填写 API 地址"}
                      </div>
                    </div>
                    <button
                      type="button"
                      data-ai-dialog-action
                      onClick={(event) => {
                        event.stopPropagation();
                        setActiveProfileId(profile.id);
                      }}
                      className={`h-[30px] px-[10px] rounded-[5px] text-[12px] border ${active ? "border-[#07c160] text-[#07c160]" : (dark ? "border-[#444] text-[#aaa] hover:bg-[#292929]" : "border-[#d6d6d6] text-[#555] hover:bg-[#f0f0f0]")}`}
                    >
                      {active ? "已选择" : "设为使用"}
                    </button>
                    <svg className={`w-[18px] h-[18px] shrink-0 transition-transform ${expanded ? "rotate-180" : ""} ${dark ? "text-[#777]" : "text-[#888]"}`} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="m6 9 6 6 6-6" />
                    </svg>
                  </div>

                  {expanded && (
                    <div className={`px-[14px] pb-[14px] pt-[12px] border-t space-y-[13px] ${dark ? "border-[#303030]" : "border-[#e8e8e8]"}`}>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-[13px]">
                        <div>
                          <label htmlFor={`ai-profile-name-${profile.id}`} className={`block text-[12px] mb-[6px] ${dark ? "text-[#999]" : "text-[#666]"}`}>配置名称</label>
                          <input
                            id={`ai-profile-name-${profile.id}`}
                            value={profile.name}
                            onChange={(event) => updateProfile(profile.id, { name: event.target.value })}
                            placeholder="例如 DeepSeek / 灵算 / OpenAI"
                            autoComplete="off"
                            className={`w-full h-[38px] rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                          />
                        </div>
                        <div>
                          <label htmlFor={`ai-model-${profile.id}`} className={`block text-[12px] mb-[6px] ${dark ? "text-[#999]" : "text-[#666]"}`}>模型</label>
                          <input
                            id={`ai-model-${profile.id}`}
                            value={profile.model}
                            onChange={(event) => updateProfile(profile.id, { model: event.target.value })}
                            placeholder="例如 gpt-5.6-sol / deepseek-v4-flash"
                            autoComplete="off"
                            className={`w-full h-[38px] rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                          />
                        </div>
                      </div>

                      <div>
                        <label htmlFor={`ai-base-url-${profile.id}`} className={`block text-[12px] mb-[6px] ${dark ? "text-[#999]" : "text-[#666]"}`}>API 地址</label>
                        <input
                          id={`ai-base-url-${profile.id}`}
                          value={profile.base_url}
                          onChange={(event) => updateProfile(profile.id, { base_url: event.target.value })}
                          placeholder="https://example.com/v1"
                          autoComplete="off"
                          className={`w-full h-[38px] rounded-[5px] border px-[10px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                        />
                      </div>

                      <div>
                        <label htmlFor={`ai-api-key-${profile.id}`} className={`block text-[12px] mb-[6px] ${dark ? "text-[#999]" : "text-[#666]"}`}>API Key</label>
                        <div className="relative">
                          <input
                            id={`ai-api-key-${profile.id}`}
                            type={showApiKey ? "text" : "password"}
                            value={profile.api_key || ""}
                            onChange={(event) => updateProfile(profile.id, { api_key: event.target.value, api_key_configured: Boolean(event.target.value.trim()) })}
                            placeholder="请输入 API Key"
                            autoComplete="new-password"
                            className={`w-full h-[38px] rounded-[5px] border pl-[10px] pr-[42px] outline-none focus:border-[#07c160] ${dark ? "border-[#393939] bg-[#111] placeholder:text-[#555]" : "border-[#d8d8d8] bg-[#fafafa] placeholder:text-[#aaa]"}`}
                          />
                          <button
                            type="button"
                            title={showApiKey ? "隐藏 API Key" : "显示 API Key"}
                            aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}
                            aria-pressed={showApiKey}
                            onClick={() => setShowApiKeyById((prev) => ({ ...prev, [profile.id]: !prev[profile.id] }))}
                            className={`absolute right-[3px] top-[3px] w-[32px] h-[32px] flex items-center justify-center rounded-[4px] ${dark ? "text-[#888] hover:text-[#ddd] hover:bg-[#252525]" : "text-[#777] hover:text-[#222] hover:bg-[#ededed]"}`}
                          >
                            {showApiKey ? (
                              <svg aria-hidden="true" className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0" />
                                <circle cx="12" cy="12" r="3" />
                              </svg>
                            ) : (
                              <svg aria-hidden="true" className="w-[18px] h-[18px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.8 10.8 0 0 1-1.444 2.49" />
                                <path strokeLinecap="round" strokeLinejoin="round" d="M14.084 14.158a3 3 0 0 1-4.242-4.242" />
                                <path strokeLinecap="round" strokeLinejoin="round" d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" />
                                <path strokeLinecap="round" strokeLinejoin="round" d="m2 2 20 20" />
                              </svg>
                            )}
                          </button>
                        </div>
                      </div>

                      {feedback && (
                        <div role="status" className={`px-[10px] py-[8px] rounded-[5px] text-[12px] ${feedback.kind === "success" ? (dark ? "bg-[#173d28] text-[#68db95]" : "bg-[#edf9f1] text-[#237844]") : (dark ? "bg-[#281919] text-[#f0a0a0]" : "bg-[#fff2f2] text-[#b74242]")}`}>
                          {feedback.message}
                        </div>
                      )}

                      <div className="flex flex-wrap items-center justify-between gap-[10px]">
                        <div className={`text-[12px] leading-[19px] ${dark ? "text-[#777]" : "text-[#888]"}`}>
                          校验只测试连接，不保存配置；保存后写入本机 config.yaml。
                        </div>
                        <div className="flex items-center gap-[8px]">
                          <button
                            type="button"
                            data-ai-dialog-action
                            disabled={savingSettings || Boolean(validatingProfileId)}
                            onClick={() => void validateProfile(profile)}
                            className={`h-[32px] px-[12px] rounded-[5px] border text-[13px] disabled:opacity-50 ${dark ? "border-[#4a4a4a] hover:bg-[#292929]" : "border-[#c9c9c9] hover:bg-[#f2f2f2]"}`}
                          >
                            {validatingProfileId === profile.id ? "校验中" : "校验"}
                          </button>
                          <button
                            type="button"
                            data-ai-dialog-action
                            disabled={savingSettings || Boolean(validatingProfileId)}
                            onClick={() => removeProfile(profile.id)}
                            className={`h-[32px] px-[12px] rounded-[5px] border text-[13px] disabled:opacity-50 ${dark ? "border-[#4a2d2d] text-[#e08a8a] hover:bg-[#2a1d1d]" : "border-[#f0caca] text-[#b74242] hover:bg-[#fff3f3]"}`}
                          >
                            删除
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {settingsFeedback && !settingsFeedback.profileId && (
            <div role="status" className={`mt-[12px] px-[10px] py-[8px] rounded-[5px] text-[12px] ${settingsFeedback.kind === "success" ? (dark ? "bg-[#173d28] text-[#68db95]" : "bg-[#edf9f1] text-[#237844]") : (dark ? "bg-[#281919] text-[#f0a0a0]" : "bg-[#fff2f2] text-[#b74242]")}`}>
              {settingsFeedback.message}
            </div>
          )}
        </div>

        <div className={`h-[58px] px-[16px] border-t flex items-center justify-end gap-[9px] ${dark ? "border-[#333]" : "border-[#e5e5e5]"}`}>
          <button type="button" disabled={savingSettings || Boolean(validatingProfileId)} onClick={onClose} className={`h-[34px] px-[14px] rounded-[5px] border text-[13px] ${dark ? "border-[#414141]" : "border-[#d0d0d0]"}`}>取消</button>
          <button type="button" disabled={savingSettings || Boolean(validatingProfileId)} onClick={() => void submit()} className="h-[34px] px-[14px] rounded-[5px] bg-[#07c160] text-white text-[13px] disabled:bg-[#315541]">{savingSettings ? "保存中" : "保存"}</button>
        </div>
      </div>
    </div>
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
          <div className="font-medium flex-1">选择聊天</div>
          <button type="button" title="关闭" onClick={onClose} className={`w-[32px] h-[32px] flex items-center justify-center ${dark ? "text-[#999]" : "text-[#666]"}`}>
            <svg className="w-[19px] h-[19px]" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24"><path strokeLinecap="round" d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <div className="p-[12px]">
          <input value={query} onChange={(event) => onQueryChange(event.target.value)} autoFocus placeholder="搜索聊天" className={`w-full h-[36px] rounded-[5px] px-[10px] outline-none border focus:border-[#07c160] ${dark ? "bg-[#151515] border-[#383838]" : "bg-[#fafafa] border-[#ddd]"}`} />
        </div>
        <div className="pane-scroll flex-1 min-h-0 overflow-y-auto">
          {targets.length === 0 ? (
            <div className={`py-[50px] text-center text-[13px] ${dark ? "text-[#666]" : "text-[#999]"}`}>没有可选聊天</div>
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
