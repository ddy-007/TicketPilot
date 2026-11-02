import { useCallback, useRef, useState } from 'react';
import { errorMessage, request } from '../api';
import type { Identity } from '../api';

export interface Session {
  token: string;
  who: Identity;
}

/**
 * Manages authentication state: login, logout, session, and busy/error/lock.
 */
export function useAuth(viewVersion: React.RefObject<number>) {
  const [session, setSession] = useState<Session | null>(null);
  const [input, setInput] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);

  const login = useCallback(async (event: React.FormEvent) => {
    event.preventDefault();
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    const version = viewVersion.current;
    const token = input; setInput('');
    try {
      const who = await request<Identity>(token, '/api/me');
      if (version !== viewVersion.current) return;
      setSession({ token, who });
    } catch (e) { if (version === viewVersion.current) setError(errorMessage(e)); }
    finally { lock.current = false; setBusy(false); }
  }, [input, viewVersion]);

  const logout = useCallback(() => {
    viewVersion.current++;
    setSession(null); setInput(''); setError('');
  }, [viewVersion]);

  return { session, input, setInput, error, setError, busy, setBusy, lock, login, logout } as const;
}
