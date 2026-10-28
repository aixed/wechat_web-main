import { useCallback, useEffect, useRef, useState } from "react";
import { clickHookLoginButton, getHookLoginQRCode, getHookLoginSettings, getHookLoginStatus, startHookWechat } from "../api";
import type { WeChatAccount } from "../types";
import { DEFAULT_AVATAR_URL } from "../avatar";

type SettingsForm = { rdv: string; proxy: { enabled: boolean; ip: string; port: string; username: string; password: string } };
const emptyForm: SettingsForm = { rdv: "", proxy: { enabled: false, ip: "", port: "", username: "", password: "" } };
type LoginPhase = "idle" | "starting" | "checking" | "waiting" | "login_ready" | "logging_in" | "success";
function accountPhase(account: WeChatAccount): LoginPhase {
  if (account.connected === false) return "idle";
  if (String(account.login_status) === "3") return "success";
  if (String(account.login_status) === "5") return "login_ready";
  if (String(account.login_status) === "2") return "logging_in";
  return ["0", "1", "4"].includes(String(account.login_status)) ? "waiting" : "checking";
}
function checked(data: any) {
  if (data?.ok === false || data?.error || data?.detail) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Hook 请求失败");
  return data;
}

export default function HookLoginPanel({ theme, onRefresh, onEnterAccount, account, autoStartNew = false, autoSelectExisting = true, onInstanceSelected }: {
  theme: "dark" | "light"; onRefresh: () => void;
  onEnterAccount?: (account: WeChatAccount) => void; account?: WeChatAccount;
  autoStartNew?: boolean; onInstanceSelected?: (id: string) => void;
  autoSelectExisting?: boolean;
}) {
  const dark = theme === "dark";
  const [settings, setSettings] = useState<any>(null);
  const [form, setForm] = useState<SettingsForm>(emptyForm);
  const [showSettings, setShowSettings] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [phase, setPhase] = useState<LoginPhase>(() => account ? accountPhase(account) : "idle");
  const [instanceId, setInstanceId] = useState("");
  const [status, setStatus] = useState("点击上方“启动微信”新增微信，或点击此处启动已保存的微信。");
  const [error, setError] = useState("");
  const [qr, setQr] = useState("");
  const [readyAccount, setReadyAccount] = useState<WeChatAccount | null>(null);
  const [loginAccount, setLoginAccount] = useState<WeChatAccount | null>(account || null);
  const [qrBusy, setQrBusy] = useState(false);
  const refreshRef = useRef(onRefresh);
  const startingRef = useRef(false);
  const autoStartRef = useRef(false);
  const qrRef = useRef('');
  const qrGeneration = useRef(0);
  const selectionGeneration = useRef(0);
  const loginRequestedAt = useRef(0);
  const clickingRef = useRef(false);
  const selectedRef = useRef(onInstanceSelected);
  selectedRef.current = onInstanceSelected;
  qrRef.current = qr;
  refreshRef.current = onRefresh;
  const clearQr = useCallback(() => {
    qrGeneration.current += 1;
    qrRef.current = '';
    setQr(''); setQrBusy(false);
  }, []);
  useEffect(() => () => { qrGeneration.current += 1; selectionGeneration.current += 1; }, []);

  const applyAccount = useCallback((current: WeChatAccount): LoginPhase => {
    let nextPhase = accountPhase(current);
    if (nextPhase === "login_ready" && loginRequestedAt.current) {
      if (Date.now() - loginRequestedAt.current < 60_000) nextPhase = "logging_in";
      else {
        loginRequestedAt.current = 0;
        setError("登录状态未变化，请确认微信窗口后重试。");
      }
    }
    setLoginAccount(current);
    setReadyAccount(nextPhase === "success" ? current : null);
    if (nextPhase !== "waiting") clearQr();
    if (nextPhase === "success") loginRequestedAt.current = 0;
    setPhase(nextPhase);
    setStatus(nextPhase === "success" ? "登陆成功，可以进入聊天。"
      : nextPhase === "logging_in" ? "登陆中..."
        : current.login_message || (nextPhase === "login_ready" ? "点击进入微信" : "等待微信登录。"));
    return nextPhase;
  }, [clearQr]);

  useEffect(() => {
    let cancelled = false;
    selectionGeneration.current += 1;
    loginRequestedAt.current = 0;
    clickingRef.current = false;
    setSettings(null);
    clearQr();
    if (!account && !autoStartNew && !autoSelectExisting) {
      setInstanceId(''); setPhase('idle');
      setStatus('请选择左侧微信，或点击上方“启动微信”。');
      return;
    }
    getHookLoginSettings(account?.id).then(checked).then((data) => {
      if (cancelled) return;
      setSettings(data);
      setForm({ rdv: data.rdv, proxy: { ...emptyForm.proxy, ...data.proxy } });
      if (data.pid && !account && !autoStartNew && autoSelectExisting) {
        setInstanceId(data.id); setPhase("checking"); selectedRef.current?.(data.id);
      }
    }).catch((err) => { if (!cancelled) setError(err.message); });
    return () => { cancelled = true; };
  }, [account?.id, autoStartNew, autoSelectExisting, clearQr]);

  useEffect(() => {
    if (!account || account.source !== "hook") return;
    setInstanceId(account.id);
    applyAccount(account);
  }, [account?.id, account?.login_status, account?.connected, account?.nickname, account?.avatar, applyAccount]);

  useEffect(() => {
    if (!instanceId || !["checking", "waiting", "login_ready", "logging_in"].includes(phase)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let qrAttempt = 0;
    const poll = async () => {
      try {
        const data = checked(await getHookLoginStatus(instanceId));
        if (cancelled) return;
        const current: WeChatAccount = data.account;
        if (!current.connected) {
          const hadQr = Boolean(qrRef.current);
          clearQr();
          if (hadQr || ["login_ready", "logging_in"].includes(phase)) {
            setPhase('idle'); setReadyAccount(null);
            setStatus('微信已退出，点击二维码区域重新启动。');
            refreshRef.current(); return;
          }
          setStatus(current.login_message || "等待微信 Hook 接口就绪。");
        } else {
          const nextPhase = applyAccount(current);
          if (nextPhase === "success") {
            refreshRef.current(); return;
          }
          if (nextPhase === "waiting" && !qrRef.current && Date.now() - qrAttempt > 5_000) {
            qrAttempt = Date.now();
            const generation = qrGeneration.current;
            const result = await getHookLoginQRCode(instanceId);
            if (!cancelled && generation === qrGeneration.current && result.ok) {
              if (result.account) applyAccount(result.account);
              if (result.qrcode) { qrRef.current = result.qrcode; setQr(result.qrcode); }
            }
          }
        }
      } catch (err) {
        if (!cancelled) setStatus(err instanceof Error ? err.message : "等待微信 Hook 接口连接。");
      }
      if (!cancelled) timer = setTimeout(poll, 2500);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [instanceId, phase, clearQr, applyAccount]);

  const enterWechat = async () => {
    if (!instanceId || phase !== "login_ready" || clickingRef.current) return;
    const generation = selectionGeneration.current;
    clickingRef.current = true;
    loginRequestedAt.current = Date.now();
    clearQr(); setError(""); setPhase("logging_in"); setStatus("登陆中...");
    try {
      const data = checked(await clickHookLoginButton(instanceId));
      if (generation !== selectionGeneration.current) return;
      if (data.account) applyAccount(data.account);
      refreshRef.current();
    } catch (err) {
      if (generation !== selectionGeneration.current) return;
      loginRequestedAt.current = 0;
      setError(err instanceof Error ? err.message : "登录请求失败");
      try {
        const data = checked(await getHookLoginStatus(instanceId));
        if (generation === selectionGeneration.current) applyAccount(data.account);
      } catch {
        if (generation === selectionGeneration.current) setPhase("login_ready");
      }
    } finally {
      if (generation === selectionGeneration.current) clickingRef.current = false;
    }
  };

  const launch = async (restart = false, newAccount = false) => {
    if (startingRef.current || !settings) return;
    if (restart && (!/^[0-9a-f]{8}$/.test(form.rdv.trim()) || parseInt(form.rdv, 16) < 0x01000000 || parseInt(form.rdv, 16) > 0x7fffffff)) {
      setError("RDV 请填写 01000000—7fffffff 范围内的 8 位小写十六进制值。"); return;
    }
    startingRef.current = true;
    loginRequestedAt.current = 0;
    setPhase("starting"); setError(""); clearQr(); setReadyAccount(null); setLoginAccount(null);
    try {
      const data = checked(await startHookWechat({ instance_id: newAccount ? "" : settings.id, restart, new: newAccount,
        ...(restart ? { rdv: form.rdv.trim(), proxy: form.proxy } : {}) }));
      setInstanceId(data.instance_id);
      const saved = checked(await getHookLoginSettings(data.instance_id));
      setSettings(saved); setForm({ rdv: saved.rdv, proxy: { ...emptyForm.proxy, ...saved.proxy } });
      setShowSettings(false); setPhase("checking"); setStatus("微信已启动，正在读取登录状态…");
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
    if (!instanceId || phase !== 'waiting' || qrBusy) return;
    const generation = qrGeneration.current;
    setQrBusy(true); setError("");
    try {
      const data = checked(await getHookLoginQRCode(instanceId, true));
      if (generation === qrGeneration.current) {
        if (data.account) {
          applyAccount(data.account);
          refreshRef.current();
        } else {
          qrRef.current = data.qrcode || ''; setQr(data.qrcode || '');
        }
      }
    } catch (err) { if (generation === qrGeneration.current) setError(err instanceof Error ? err.message : "二维码暂未就绪"); }
    finally { if (generation === qrGeneration.current) setQrBusy(false); }
  }, [instanceId, phase, qrBusy, applyAccount]);

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
          <button type="button" aria-label="微信启动设置" title="RDV 与代理设置" disabled={!settings || phase === "starting" || phase === "logging_in"} onClick={() => {
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
          <p className="text-xs leading-5 text-[#888]">确定后将关闭当前微信，按新参数启动并读取登录状态。</p>
          <div className="flex gap-3"><button type="submit" disabled={phase === "starting"} className="flex-1 rounded bg-[#07c160] px-4 py-2 text-sm text-white disabled:opacity-50">{phase === "starting" ? "正在重新启动…" : "确定并重新启动"}</button><button type="button" disabled={phase === "starting"} onClick={() => setShowSettings(false)} className="rounded border border-[#666] px-4 py-2 text-sm">取消</button></div>
        </form> : <>
          {["login_ready", "logging_in", "success"].includes(phase) ? <div className="flex flex-col items-center gap-4 py-5">
            <img src={loginAccount?.avatar || DEFAULT_AVATAR_URL} alt="微信头像" className="h-24 w-24 rounded-lg object-cover" onError={(event) => {
              if (event.currentTarget.src !== DEFAULT_AVATAR_URL) event.currentTarget.src = DEFAULT_AVATAR_URL;
            }} />
            <div className="max-w-full truncate text-xl">{loginAccount?.nickname || "微信账号"}</div>
            {phase !== "success" && <button type="button" disabled={phase === "logging_in" || !settings} onClick={() => void enterWechat()} className="mt-2 min-w-[160px] rounded bg-[#07c160] px-6 py-2.5 text-white disabled:cursor-default disabled:opacity-60">
              {phase === "logging_in" ? "登陆中..." : "进入微信"}
            </button>}
          </div> : <button type="button" aria-label={phase === 'idle' ? '启动此微信' : phase === 'waiting' ? '刷新当前微信二维码' : '读取微信登录状态'} title={phase === "waiting" ? "点击刷新二维码，每 2 分钟自动刷新" : "读取微信登录状态"} disabled={!settings || phase === 'starting' || phase === 'checking' || qrBusy} onClick={() => void (phase === 'idle' ? launch() : refreshQr())} className={`relative mx-auto flex h-[232px] w-[232px] items-center justify-center rounded disabled:cursor-default ${qr ? "bg-white p-2" : dark ? "bg-[#191919]" : "bg-[#f4f4f4]"}`}>
            {phase === 'waiting' && qr ? <img src={qr} alt={`微信登录二维码 ${instanceId}`} className={`h-full w-full ${qrBusy ? 'opacity-40' : ''}`} /> : <span className="px-4 text-sm text-[#888]">{phase === "starting" || phase === "checking" ? "正在读取登录状态…" : phase === "waiting" ? "二维码准备中…" : settings ? "点击启动微信" : "请选择或启动微信"}</span>}
            {qrBusy && <span className="absolute inset-0 flex items-center justify-center text-sm text-[#555]">正在刷新…</span>}
          </button>}
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
