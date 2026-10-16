/**
 * Short confirmations after an action ("Department added"). They are announced to screen
 * readers through a polite live region and disappear on their own; errors stay on the page
 * next to what failed instead of in a toast.
 *
 * A message that is already showing is not stacked again: the visible one is replaced and its
 * time restarts (so five quick "Preference saved" confirmations show as one). At most three
 * different messages show at once.
 */
import { CircleCheck, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

interface ToastItem {
  id: number;
  message: string;
}

type Notify = (message: string) => void;

const ToastContext = createContext<Notify | null>(null);

const VISIBLE_MS = 5000;

export function useToast(): Notify {
  const notify = useContext(ToastContext);
  if (notify === null) {
    throw new Error("useToast must be used inside ToastProvider");
  }
  return notify;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);
  const notify = useCallback<Notify>((message) => {
    const id = next.current++;
    setItems((current) => [...current.filter((item) => item.message !== message).slice(-2), { id, message }]);
  }, []);
  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div
        role="status"
        aria-live="polite"
        className="pointer-events-none fixed inset-x-4 bottom-20 z-50 flex flex-col items-center gap-2 sm:inset-x-auto sm:bottom-6 sm:right-6 sm:items-end"
      >
        {items.map((item) => (
          <Toast key={item.id} item={item} onDismiss={dismiss} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function Toast({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => {
      onDismiss(item.id);
    }, VISIBLE_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [item.id, onDismiss]);
  return (
    <div className="pointer-events-auto flex max-w-sm items-start gap-2 rounded-[var(--radius-surface)] bg-neutral-900 px-4 py-3 text-sm text-neutral-0 shadow-[var(--shadow-float)]">
      <CircleCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-success-50" />
      <p className="flex-1">{item.message}</p>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          onDismiss(item.id);
        }}
        className="rounded p-0.5 text-neutral-300 hover:text-neutral-0"
      >
        <X aria-hidden className="size-4" />
      </button>
    </div>
  );
}
