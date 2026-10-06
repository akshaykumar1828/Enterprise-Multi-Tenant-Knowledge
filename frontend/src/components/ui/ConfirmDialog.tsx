import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";

export interface ConfirmOptions {
  title: string;
  message: ReactNode;
  confirmLabel: string;
  /** Destructive actions get the danger style. */
  danger?: boolean;
}

interface Pending extends ConfirmOptions {
  resolve: (confirmed: boolean) => void;
}

/**
 * An accessible confirmation dialog (role="alertdialog", focus moved in and restored, Escape and
 * Cancel decline, Tab stays inside). Usage:
 *   const { confirm, dialog } = useConfirm();
 *   if (await confirm({...})) { ... }   and render {dialog}.
 */
export function useConfirm() {
  const [pending, setPending] = useState<Pending | null>(null);

  const confirm = useCallback(
    (options: ConfirmOptions) => new Promise<boolean>((resolve) => setPending({ ...options, resolve })),
    [],
  );
  const close = useCallback(
    (confirmed: boolean) => {
      pending?.resolve(confirmed);
      setPending(null);
    },
    [pending],
  );

  const dialog = pending ? <ConfirmDialog options={pending} onClose={close} /> : null;
  return { confirm, dialog };
}

function ConfirmDialog({ options, onClose }: { options: ConfirmOptions; onClose: (confirmed: boolean) => void }) {
  const titleId = useId();
  const messageId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    cancel.current?.focus(); // the safe choice is focused first
    return () => previous?.focus?.();
  }, []);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose(false);
    } else if (event.key === "Tab" && panel.current) {
      const buttons = Array.from(panel.current.querySelectorAll<HTMLButtonElement>("button"));
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose(false)}>
      <div ref={panel} className="dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId}
           aria-describedby={messageId} onKeyDown={onKeyDown}>
        <h2 id={titleId} className="dialog__title">{options.title}</h2>
        <div id={messageId} className="dialog__message">{options.message}</div>
        <div className="dialog__actions">
          <button ref={cancel} type="button" className="button button--ghost" onClick={() => onClose(false)}>
            Cancel
          </button>
          <button type="button" className={`button ${options.danger ? "button--danger" : ""}`} onClick={() => onClose(true)}>
            {options.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
