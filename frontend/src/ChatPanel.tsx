import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api.ts";
import { errorText } from "./errors.ts";
import { S } from "./strings.ts";
import type { ChatMessageView, ChatReply } from "./types.ts";

interface Props {
  onCards: (cardIds: string[]) => void;
  onChange: () => void;
}

function chronological(messages: ChatMessageView[]): ChatMessageView[] {
  return [...messages].sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/**
 * Shows the stored (masked) history and the server's fact lines. The text a customer types is
 * never echoed from the input box: the bubble comes from the history the server returns.
 */
export function ChatPanel({ onCards, onChange }: Props) {
  const [messages, setMessages] = useState<ChatMessageView[]>([]);
  const [reply, setReply] = useState<ChatReply | null>(null);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const loadHistory = useCallback(async () => {
    setMessages(chronological((await api.chatHistory()).messages));
  }, []);

  useEffect(() => {
    let current = true;
    api
      .chatHistory()
      .then((view) => {
        if (current) setMessages(chronological(view.messages));
      })
      .catch(() => {
        if (current) setError(S.chat.unavailable);
      });
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, reply, sending]);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const outgoing = text.trim();
    if (!outgoing || sending) return;
    setSending(true);
    setError(null);
    setText("");
    try {
      const answer = await api.sendChat(outgoing);
      setReply(answer);
      if (answer.card_ids.length > 0) onCards(answer.card_ids);
      await loadHistory();
      onChange();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSending(false);
    }
  };

  // The newest assistant message is drawn from the structured reply, so it is not drawn twice.
  const last = messages[messages.length - 1];
  const shown = reply && last?.role === "assistant" ? messages.slice(0, -1) : messages;

  return (
    <section className="chat" aria-label={S.chat.title}>
      <h2>{S.chat.title}</h2>
      <div className="messages" data-testid="chat-messages">
        {messages.length === 0 && !reply && <p className="muted">{S.chat.empty}</p>}
        {shown.map((m, i) => (
          <div key={`${i}-${m.created_at}`} className={`bubble bubble-${m.role}`}>
            {m.text}
          </div>
        ))}
        {sending && <div className="bubble bubble-user muted">{S.chat.pendingUser}</div>}
        {reply && <ReplyBubble reply={reply} onCards={onCards} />}
        <div ref={endRef} />
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <form onSubmit={send} className="chat-form">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={S.chat.placeholder}
          rows={2}
          maxLength={2000}
          disabled={sending}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button type="submit" disabled={sending || !text.trim()}>
          {sending ? S.chat.sending : S.chat.send}
        </button>
      </form>
      <p className="muted small">{S.chat.codeNotice}</p>
    </section>
  );
}

function ReplyBubble({ reply, onCards }: { reply: ChatReply; onCards: (cardIds: string[]) => void }) {
  return (
    <div className="bubble bubble-assistant" data-testid="chat-reply">
      {reply.fact_lines.length > 0 && (
        <ul className="fact-lines">
          {reply.fact_lines.map((line, i) => (
            <li key={`${i}-${line}`}>{line}</li>
          ))}
        </ul>
      )}
      {reply.explanation && <p className="explanation">{reply.explanation}</p>}
      {reply.explanation_replaced && <p className="muted small">{S.chat.replaced}</p>}
      {reply.card_ids.length > 0 && (
        <button type="button" className="secondary" onClick={() => onCards(reply.card_ids)}>
          {S.chat.openCard}
        </button>
      )}
    </div>
  );
}
