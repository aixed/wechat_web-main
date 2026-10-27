import { useEffect, useState } from 'react';
import { fetchJSON } from './api';

interface ClientSettings {
  public_assets: { wecom_icon_url?: string };
  workflow: { self_mention_names: string[] };
}

let pending: Promise<ClientSettings> | null = null;

export function useClientSettings(): ClientSettings | null {
  const [settings, setSettings] = useState<ClientSettings | null>(null);
  useEffect(() => {
    let active = true;
    pending ||= fetchJSON('/api/client-settings').then((data) => {
      if (!data?.ok) throw new Error('Client configuration unavailable');
      return data as ClientSettings;
    });
    pending.then((data) => { if (active) setSettings(data); }).catch(() => { pending = null; });
    return () => { active = false; };
  }, []);
  return settings;
}
