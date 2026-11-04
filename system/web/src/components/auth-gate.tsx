import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowRight, Layers2, Loader2 } from "lucide-react";
import { SESSION_EXPIRED_EVENT } from "@/lib/api";
import { authApi, type AuthSession } from "@/lib/auth";
import { Button } from "./ui/button";

/** Protected pages mount only after the server has confirmed the session. */
export function AuthGate({ children }: {
  children: (session: AuthSession, logout: () => Promise<void>, loggingOut: boolean) => ReactNode;
}) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [connectionError, setConnectionError] = useState(false);
  const [check, setCheck] = useState(0);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setChecking(true); setConnectionError(false);
    authApi.session(controller.signal).then(value => {
      if (!controller.signal.aborted) setSession(value);
    }).catch(() => {
      if (!controller.signal.aborted) setConnectionError(true);
    }).finally(() => {
      if (!controller.signal.aborted) setChecking(false);
    });
    return () => controller.abort();
  }, [check]);

  useEffect(() => {
    const expired = () => {
      setSession({ enabled: true, authenticated: false, username: null });
      setPassword(""); setError("登录已过期，请重新登录。");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }, []);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError(null);
    try {
      const value = await authApi.login(username.trim(), password);
      if (!value.authenticated) throw new Error("登录失败，请重试。");
      setSession(value); setPassword("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "登录失败，请重试。"); }
    finally { submitting.current = false; setBusy(false); }
  }
  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try { setSession(await authApi.logout()); setPassword(""); setError(null); }
    finally { setLoggingOut(false); }
  }

  if (!checking && !connectionError && session?.authenticated) return <>{children(session, logout, loggingOut)}</>;

  return <main className="auth-page">
    <section className="auth-panel" aria-label="登录 Agendex">
      <div className="auth-brand"><span className="brand-mark"><Layers2 size={21} strokeWidth={1.9} /></span><strong>Agendex<span>.</span></strong></div>
      {checking ? <div className="auth-connecting" role="status"><Loader2 size={16} className="spin" />正在连接…</div>
        : connectionError ? <div className="auth-connection-error"><h1>暂时无法连接服务</h1><Button variant="outline" onClick={() => setCheck(value => value + 1)}>重新连接</Button></div>
          : <>
            <div className="auth-heading"><h1>登录</h1><p>访问 Agent 目录与分类工作台。</p></div>
            <form onSubmit={login} className="auth-form">
              <div className="form-field"><label htmlFor="auth-username">账号</label><input id="auth-username" className="input" name="username" autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false} autoFocus required maxLength={255} value={username} disabled={busy} onChange={event => setUsername(event.target.value)} /></div>
              <div className="form-field"><label htmlFor="auth-password">密码</label><input id="auth-password" className="input" name="password" type="password" autoComplete="current-password" required maxLength={4096} value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /></div>
              {error && <p className="auth-error" role="alert">{error}</p>}
              <Button type="submit" disabled={busy || !username.trim() || !password}>{busy ? <Loader2 size={14} className="spin" /> : null}{busy ? "正在登录" : "登录"}{!busy && <ArrowRight size={14} />}</Button>
            </form>
          </>}
    </section>
  </main>;
}
