import React, { useCallback } from 'react';

interface LoginFormProps {
  runId: string;
  input: string;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  busy: boolean;
  error: string;
  readError: string;
  login: (event: React.FormEvent) => Promise<void>;
}

/** Login form shown when no session is active. */
const LoginForm = React.memo(function LoginForm({ runId, input, setInput, busy, error, readError, login }: LoginFormProps) {
  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => setInput(e.target.value), [setInput]);

  return (
    <>
      {(error || readError) && <p className="error" role="alert">{[error, readError].filter(Boolean).join(' ')}</p>}
      <section className="login"><p className="eyebrow">本机合成数据演示</p><h1>连接工作台</h1>
        <p>输入操作员或审批员 Token。身份由服务端确认，Token 只保留在当前页面内存，刷新后需要重新输入。</p>
        {runId && <p>登录后恢复任务 <code>{runId}</code>，不会创建新任务。</p>}
        <form onSubmit={login}><label htmlFor="token">角色 Token</label><input id="token" type="password" autoComplete="off" spellCheck={false} value={input} onChange={handleChange} maxLength={256} required />
          <button className="primary" disabled={busy}>{busy ? '验证中…' : '连接'}</button></form>
      </section>
    </>
  );
});

export default LoginForm;
