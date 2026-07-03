import { useEffect, useRef, useCallback, useState } from "react";
import type { WSMessage } from "./types";
import { getAccessKey, getActiveAgentId } from "./api";

export function useWebSocket(onMessage: (msg: WSMessage) => void, enabled = true) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const heartbeatTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);
  const connectingRef = useRef(false);

  const clearTimers = useCallback(() => {
    if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
    if (connectTimeoutRef.current) { clearTimeout(connectTimeoutRef.current); connectTimeoutRef.current = null; }
    if (heartbeatTimerRef.current) { clearInterval(heartbeatTimerRef.current); heartbeatTimerRef.current = null; }
    if (heartbeatTimeoutRef.current) { clearTimeout(heartbeatTimeoutRef.current); heartbeatTimeoutRef.current = null; }
  }, []);

  const connect = useCallback(() => {
    if (!enabled) return;
    if (unmountedRef.current) return;
    if (connectingRef.current) return;
    connectingRef.current = true;
    clearTimers();

    // Tear down previous socket without triggering auto-reconnect
    if (wsRef.current) {
      const old = wsRef.current;
      old.onclose = null; old.onerror = null; old.onmessage = null; old.onopen = null;
      try { old.close(); } catch { /* ignore */ }
      wsRef.current = null;
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    params.set("key", getAccessKey());
    const agentId = getActiveAgentId();
    if (agentId) params.set("agent_id", agentId);
    const wsUrl = `${protocol}//${window.location.host}/api/ws?${params.toString()}`;
    console.log("[WS] Connecting to", wsUrl);

    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      console.error("[WS] Failed to create WebSocket:", e);
      connectingRef.current = false;
      if (!unmountedRef.current) {
        reconnectTimer.current = setTimeout(connect, 2000);
      }
      return;
    }

    // If stuck in CONNECTING for 10s, abort and retry
    connectTimeoutRef.current = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) {
        console.log("[WS] Connection timeout — retrying");
        ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.onopen = null;
        try { ws.close(); } catch { /* ignore */ }
        if (wsRef.current === ws) wsRef.current = null;
        connectingRef.current = false;
        if (!unmountedRef.current) {
          reconnectTimer.current = setTimeout(connect, 2000);
        }
      }
    }, 10000);

    ws.onopen = () => {
      console.log("[WS] Connected");
      if (connectTimeoutRef.current) { clearTimeout(connectTimeoutRef.current); connectTimeoutRef.current = null; }
      connectingRef.current = false;
      setConnected(true);

      const sendHeartbeat = () => {
        if (unmountedRef.current || wsRef.current !== ws) return;
        if (ws.readyState !== WebSocket.OPEN) {
          try { ws.close(); } catch { /* ignore */ }
          return;
        }
        if (heartbeatTimeoutRef.current) {
          console.log("[WS] Previous ping still pending — reconnecting");
          try { ws.close(); } catch { /* ignore */ }
          return;
        }
        try {
          ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
        } catch {
          try { ws.close(); } catch { /* ignore */ }
          return;
        }
        heartbeatTimeoutRef.current = setTimeout(() => {
          if (wsRef.current === ws && ws.readyState === WebSocket.OPEN) {
            console.log("[WS] Ping timeout — reconnecting");
            try { ws.close(); } catch { /* ignore */ }
          }
        }, 8000);
      };

      heartbeatTimerRef.current = setInterval(sendHeartbeat, 25000);
      setTimeout(sendHeartbeat, 1000);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WSMessage | { type?: string };
        if (data?.type === "pong") {
          if (heartbeatTimeoutRef.current) {
            clearTimeout(heartbeatTimeoutRef.current);
            heartbeatTimeoutRef.current = null;
          }
          return;
        }
        onMessageRef.current(data as WSMessage);
      } catch (e) {
        console.error("[WS] Parse error:", e);
      }
    };

    ws.onclose = () => {
      console.log("[WS] Disconnected");
      if (connectTimeoutRef.current) { clearTimeout(connectTimeoutRef.current); connectTimeoutRef.current = null; }
      connectingRef.current = false;
      setConnected(false);
      if (!unmountedRef.current) {
        clearTimers();
        reconnectTimer.current = setTimeout(connect, 2000);
      }
    };

    ws.onerror = (err) => {
      console.error("[WS] Error:", err);
      ws.close();
    };

    wsRef.current = ws;
  }, [clearTimers, enabled]);

  const forceReconnect = useCallback(() => {
    console.log("[WS] Force reconnect");
    connectingRef.current = false;
    connect();
  }, [connect]);

  useEffect(() => {
    unmountedRef.current = false;
    if (!enabled) {
      clearTimers();
      if (wsRef.current) {
        const ws = wsRef.current;
        ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.onopen = null;
        try { ws.close(); } catch { /* ignore */ }
        wsRef.current = null;
      }
      setConnected(false);
      return;
    }

    // Small delay so React strict-mode cleanup finishes before we connect
    const initTimer = setTimeout(() => {
      if (!unmountedRef.current) connect();
    }, 150);

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          forceReconnect();
        } else {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch { forceReconnect(); return; }
        }
      }
    };

    const onPageShow = (e: PageTransitionEvent) => {
      if (e.persisted) { console.log("[WS] Restored from bfcache"); forceReconnect(); }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pageshow", onPageShow);

    return () => {
      unmountedRef.current = true;
      connectingRef.current = false;
      clearTimeout(initTimer);
      clearTimers();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pageshow", onPageShow);
      if (wsRef.current) {
        const ws = wsRef.current;
        ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.onopen = null;
        try { ws.close(); } catch { /* ignore */ }
        wsRef.current = null;
      }
    };
  }, [connect, forceReconnect, clearTimers, enabled]);

  return { connected, ws: wsRef };
}
