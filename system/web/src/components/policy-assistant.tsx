import { useEffect, useRef, useState } from "react";
import { Loader2, MessageSquare, Send } from "lucide-react";
import { api, type PolicyChatMessage, type PolicyDraft } from "@/lib/api";
import { policyValidation } from "@/lib/domain";
import { Button } from "./ui/button";
import { InlineError } from "./shared";

export const policyAssistantExample = "按应用场景分类，包含 Coding Agent、Penetration Testing Agent 和 Browser Agent。允许一个仓库有多个标签，初始阈值设为 0.5。";

/** Generated text only edits the draft. Saving a policy and starting a task stay explicit. */
export function PolicyAssistant({ draft, configured, disabled, onGenerated, onBusyChange }: {
  draft: PolicyDraft; configured: boolean; disabled?: boolean;
  onGenerated: (draft: PolicyDraft) => void; onBusyChange: (busy: boolean) => void;
}) {
  const [messages, setMessages] = useState<PolicyChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const active = useRef(true);
  const busyGuard = useRef(false);
  const conversation = useRef<HTMLDivElement>(null);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; controller.current?.abort(); onBusyChange(false); };
  }, []);
  useEffect(() => { if (conversation.current) conversation.current.scrollTop = conversation.current.scrollHeight; }, [messages, busy]);

  async function generate(outgoing: PolicyChatMessage[]) {
    if (busyGuard.current || disabled || !configured || !outgoing.length) return;
    busyGuard.current = true; setBusy(true); onBusyChange(true); setError(null);
    controller.current = new AbortController();
    try {
      const result = await api.generatePolicy(outgoing, draft, controller.current.signal);
      if (!active.current || controller.current.signal.aborted) return;
      const candidate: PolicyDraft = { ...result.policy,
        tags: result.policy.tags.map(tag => ({ ...tag, id: tag.id || crypto.randomUUID(), threshold: tag.threshold ?? result.policy.threshold })),
      };
      const invalid = policyValidation(candidate);
      if (invalid) throw new Error(`生成的策略不完整：${invalid}`);
      setMessages([...outgoing, { role: "assistant", content: result.message || "已填写下方策略，可继续调整。" }]);
      onGenerated(candidate);
    } catch (reason) {
      if (active.current && !controller.current.signal.aborted) setError(reason instanceof Error ? reason.message : "策略生成失败，请重试");
    } finally {
      busyGuard.current = false;
      if (active.current) { setBusy(false); onBusyChange(false); }
    }
  }
  function send() {
    const content = input.trim();
    if (!content) return;
    const outgoing: PolicyChatMessage[] = [...messages, { role: "user", content }];
    setMessages(outgoing); setInput(""); void generate(outgoing);
  }
  return <section className="policy-assistant" aria-label="GLM 5.3 策略助手">
    <div className="policy-assistant-heading"><span><MessageSquare size={14} />GLM 5.3 策略助手</span><Button type="button" size="sm" variant="ghost" disabled={busy || disabled} onClick={() => setInput(policyAssistantExample)}>填入示例</Button></div>
    {messages.length > 0 && <div className="policy-conversation" role="log" aria-live="polite" ref={conversation}>{messages.map((message, index) => <div className={`policy-message message-${message.role}`} key={index}><span>{message.role === "user" ? "你" : "GLM 5.3"}</span><p>{message.content}</p></div>)}</div>}
    <div className="policy-assistant-composer"><textarea className="textarea" rows={2} aria-label="自然语言策略需求" placeholder="描述你希望如何分类，例如按应用场景区分 Coding、渗透测试和网页操作…" value={input} maxLength={6000} disabled={busy || disabled} onChange={event => setInput(event.target.value)} /><Button type="button" size="sm" disabled={busy || disabled || !configured || !input.trim()} onClick={send}>{busy ? <Loader2 size={14} className="spin" /> : <Send size={13} />}{busy ? "生成中" : "生成策略"}</Button></div>
    {!configured && <p className="assistant-connection">GLM 5.3 尚未配置</p>}
    <InlineError message={error} />{error && <Button type="button" size="sm" variant="ghost" disabled={busy || disabled || !configured} onClick={() => void generate(messages)}>重试</Button>}
  </section>;
}
