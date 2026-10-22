import { useCallback, useEffect, useRef, useState } from "react";
import { getHookLoginQRCode, getHookLoginSettings, getHookLoginStatus, startHookWechat } from "../api";
import type { WeChatAccount } from "../types";

type SettingsForm = { rdv: string; proxy: { enabled: boolean; ip: string; port: string; username: string; password: string } };
const emptyForm: SettingsForm = { rdv: "", proxy: { enabled: false, ip: "", port: "", username: "", password: "" } };
function checked(data: any) {
  if (data?.ok === false || data?.error || data?.detail) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Hook 请求失败");
  return data;
}

export default function HookLoginPanel({ theme, onRefresh, onEnterAccount, account, autoStartNew = false, onInstanceSelected }: {
  theme: "dark" | "light"; onRefresh: () => void;
  onEnterAccount?: (account: WeChatAccount) => void; account?: WeChatAccount;
  autoStartNew?: boolean; onInstanceSelected?: (id: string) => void;
}) {
  const dark = theme === "dark";
  const [settings, setSettings] = useState<any>(null);
  const [form, setForm] = useState<SettingsForm>(emptyForm);
  const [showSettings, setShowSettings] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [phase, setPhase] = useState<"idle" | "starting" | "waiting" | "success">("idle");
  const [instanceId, setInstanceId] = useState("");
  const [status, setStatus] = useState("点击上方“启动微信”新增微信，或点击此处启动已保存的微信。");
  const [error, setError] = useState("");
  const [qr, setQr] = useState("");
  const [readyAccount, setReadyAccount] = useState<WeChatAccount | null>(null);
  const [qrBusy, setQrBusy] = useState(false);
  const refreshRef = useRef(onRefresh);
  const startingRef = useRef(false);
  const autoStartRef = useRef(false);
  const qrRef = useRef('');
  const selectedRef = useRef(onInstanceSelected);
  selectedRef.current = onInstanceSelected;
  qrRef.current = qr;
  refreshRef.current = onRefresh;

  useEffect(() => {
    let cancelled = false;
    setSettings(null);
    getHookLoginSettings(account?.id).then(checked).then((data) => {
      if (cancelled) return;
      setSettings(data);
      setForm({ rdv: data.rdv, proxy: { ...emptyForm.proxy, ...data.proxy } });
      if (data.pid && !account && !autoStartNew) {
        setInstanceId(data.id); setPhase("waiting"); selectedRef.current?.(data.id);
      }
    }).catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [account?.id]);

  useEffect(() => {
    if (!account || account.source !== "hook") return;
    setInstanceId(account.id);
    setReadyAccount(account.login_status === "3" ? account : null);
    setPhase(account.login_status === "3" ? "success" : account.connected ? "waiting" : "idle");
    setStatus(account.login_status === "3" ? "微信已登录，可以进入聊天。" : account.connected ? "等待微信登录。" : "点击二维码区域重新启动此微信。");
  }, [account?.id, account?.login_status, account?.connected]);

  useEffect(() => {
    if (!instanceId || phase !== "waiting") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let qrAttempt = 0;
    const poll = async () => {
      try {
        const data = checked(await getHookLoginStatus(instanceId));
        if (cancelled) return;
        const current: WeChatAccount = data.account;
        if (current.login_status === "3") {
          setReadyAccount(current); setPhase("success"); setStatus("微信已登录，可以进入聊天。"); setQr("");
          refreshRef.current(); return;
        }
        setStatus(current.login_message || "等待微信启动及 Hook 接口就绪。");
        if (current.connected && !qrRef.current && Date.now() - qrAttempt > 5_000) {
          qrAttempt = Date.now();
          const result = await getHookLoginQRCode(instanceId);
          if (!cancelled && result.ok && result.qrcode) setQr(result.qrcode);
        }
      } catch (err) {
        if (!cancelled) setStatus(err instanceof Error ? err.message : "等待微信 Hook 接口连接。");
      }
      if (!cancelled) timer = setTimeout(poll, 2500);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [instanceId, phase]);

  const launch = async (restart = false, newAccount = false) => {
    if (startingRef.current || !settings) return;
    if (restart && (!/^[0-9a-f]{8}$/.test(form.rdv.trim()) || parseInt(form.rdv, 16) < 0x01000000 || parseInt(form.rdv, 16) > 0x7fffffff)) {
      setError("RDV 请填写 01000000—7fffffff 范围内的 8 位小写十六进制值。"); return;
    }
    startingRef.current = true;
    setPhase("starting"); setError(""); setQr(""); setReadyAccount(null);
    try {
      const data = checked(await startHookWechat({ instance_id: newAccount ? "" : settings.id, restart, new: newAccount,
        ...(restart ? { rdv: form.rdv.trim(), proxy: form.proxy } : {}) }));
      setInstanceId(data.instance_id);
      const saved = checked(await getHookLoginSettings(data.instance_id));
      setSettings(saved); setForm({ rdv: saved.rdv, proxy: { ...emptyForm.proxy, ...saved.proxy } });
      setShowSettings(false); setPhase("waiting"); setStatus("微信已启动，正在获取二维码…");
      selectedRef.current?.(data.instance_id);
      refreshRef.current();
    } catch (err) {
      setPhase("idle"); setError(err instanceof Error ? err.message : "启动微信失败");
    } finally { startingRef.current = false; }
  };

  useEffect(() => {
    if (settings && autoStartNew && !autoStartRef.current) {
      autoStartRef.current = true;
      void launch(false, true);
    }
  }, [settings, autoStartNew]);

  const refreshQr = useCallback(async () => {
    if (!instanceId || qrBusy) return;
    setQrBusy(true); setError("");
    try { setQr(checked(await getHookLoginQRCode(instanceId, true)).qrcode || ""); }
    catch (err) { setError(err instanceof Error ? err.message : "二维码暂未就绪"); }
    finally { setQrBusy(false); }
  }, [instanceId, qrBusy]);

  useEffect(() => {
    if (!instanceId || phase !== 'waiting') return;
    const timer = setInterval(() => { void refreshQr(); }, 120_000);
    return () => clearInterval(timer);
  }, [instanceId, phase, refreshQr]);

  const inputClass = `mt-2 w-full rounded-md border px-3 py-2 text-sm outline-none focus:border-[#07c160] ${dark ? "bg-[#292929] border-[#444] text-[#eee]" : "bg-white border-[#ccc] text-[#222]"}`;
  const updateProxy = (key: string, value: string | boolean) => setForm((current) => ({ ...current, proxy: { ...current.proxy, [key]: value } }));
  return (
    <div className={`h-full overflow-y-auto p-6 ${dark ? "bg-[#111] text-[#ddd]" : "bg-[#f4f4f4] text-[#222]"}`}>
      <div className={`relative mx-auto max-w-[420px] space-y-5 rounded-lg border p-6 ${dark ? "bg-[#202020] border-[#333]" : "bg-white border-[#ddd]"}`}>
        <div className="flex items-center justify-between">
          <h2 className="text-xl">{showSettings ? "微信启动设置" : "微信登录"}</h2>
          <button type="button" aria-label="微信启动设置" title="RDV 与代理设置" disabled={!settings || phase === "starting"} onClick={() => {
            setForm({ rdv: settings.rdv, proxy: { ...emptyForm.proxy, ...settings.proxy } }); setError(""); setShowSettings(!showSettings);
          }} className="rounded p-2 text-[#999] hover:text-[#07c160] disabled:opacity-40">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m9.5 3-.7 2.1-2 .9-2.1-.5-2.4 4.1L3.8 11v2l-1.5 1.4 2.4 4.1 2.1-.5 2 .9.7 2.1h5l.7-2.1 2-.9 2.1.5 2.4-4.1-1.5-1.4v-2l1.5-1.4-2.4-4.1-2.1.5-2-.9-.7-2.1z"/><circle cx="12" cy="12" r="3.2"/></svg>
          </button>
        </div>
        {showSettings ? <form onSubmit={(event) => { event.preventDefault(); void launch(true); }} className="space-y-5">
          <label className="block text-sm">RDV<input aria-label="Hook RDV" required maxLength={8} value={form.rdv} onChange={(event) => setForm({ ...form, rdv: event.target.value })} className={inputClass} /><span className="mt-2 block text-xs text-[#888]">已为当前微信保存。范围：01000000～7fffffff。</span></label>
          <div className="flex items-center justify-between text-sm"><span>使用网络代理</span><button type="button" role="switch" aria-label="使用网络代理" aria-checked={form.proxy.enabled} onClick={() => updateProxy("enabled", !form.proxy.enabled)} className={`relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#07c160] ${form.proxy.enabled ? "bg-[#07c160]" : dark ? "bg-[#555]" : "bg-[#c7c7c7]"}`}><span className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 ${form.proxy.enabled ? "translate-x-5" : "translate-x-0"}`} /></button></div>
          {form.proxy.enabled && <div className="space-y-4">
            <label className="block text-sm">地址<input aria-label="代理地址" required value={form.proxy.ip} onChange={(event) => updateProxy("ip", event.target.value)} className={inputClass} /></label>
            <label className="block text-sm">端口<input aria-label="代理端口" required type="number" min="1" max="65535" value={form.proxy.port} onChange={(event) => updateProxy("port", event.target.value)} className={inputClass} /></label>
            {!advanced ? <button type="button" onClick={() => setAdvanced(true)} className="block ml-auto text-sm text-[#07c160]">更多设置</button> : <>
              <label className="block text-sm">账户<input aria-label="代理账户" autoComplete="off" value={form.proxy.username} onChange={(event) => updateProxy("username", event.target.value)} className={inputClass} /></label>
              <label className="block text-sm">密码<input aria-label="代理密码" type="password" autoComplete="new-password" value={form.proxy.password} onChange={(event) => updateProxy("password", event.target.value)} className={inputClass} /></label>
            </>}
          </div>}
          <p className="text-xs leading-5 text-[#888]">确定后将关闭当前微信，按新参数启动并获取二维码。</p>
          <div className="flex gap-3"><button type="submit" disabled={phase === "starting"} className="flex-1 rounded bg-[#07c160] px-4 py-2 text-sm text-white disabled:opacity-50">{phase === "starting" ? "正在重新启动…" : "确定并重新启动"}</button><button type="button" disabled={phase === "starting"} onClick={() => setShowSettings(false)} className="rounded border border-[#666] px-4 py-2 text-sm">取消</button></div>
        </form> : <>
          <button type="button" aria-label={phase === 'idle' ? '启动此微信' : '刷新当前微信二维码'} title="点击刷新二维码，每 2 分钟自动刷新" disabled={!settings || phase === 'starting' || phase === 'success' || qrBusy} onClick={() => void (phase === 'idle' ? launch() : refreshQr())} className={`relative mx-auto flex h-[232px] w-[232px] items-center justify-center rounded disabled:cursor-default ${qr ? "bg-white p-2" : dark ? "bg-[#191919]" : "bg-[#f4f4f4]"}`}>
            {qr ? <img src={qr} alt={`微信登录二维码 ${instanceId}`} className={`h-full w-full ${qrBusy ? 'opacity-40' : ''}`} /> : <span className="px-4 text-sm text-[#888]">{phase === "success" ? "微信已登录" : phase === "starting" || phase === "waiting" ? "二维码准备中…" : "点击启动微信"}</span>}
            {qrBusy && <span className="absolute inset-0 flex items-center justify-center text-sm text-[#555]">正在刷新…</span>}
          </button>
          <p role="status" className={`text-center text-sm ${phase === "success" ? "text-[#07c160]" : "text-[#999]"}`}>{status}</p>
          {settings && <p className="text-center text-xs text-[#888]">RDV {settings.rdv} · Port {settings.start_port} · {settings.proxy?.enabled ? "使用代理" : "未使用代理"}</p>}
          {phase === 'waiting' && <p className="text-center text-xs text-[#777]">点击二维码刷新 · 每 2 分钟自动刷新</p>}
          <div className="flex justify-center gap-3">
            {readyAccount && onEnterAccount && <button type="button" onClick={() => onEnterAccount(readyAccount)} className="rounded bg-[#07c160] px-5 py-2 text-sm text-white">进入聊天</button>}
          </div>
        </>}
        {error && <p role="alert" className="text-sm leading-5 text-[#ef7777]">{error}</p>}
      </div>
    </div>
  );
}
