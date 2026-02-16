"use client";

import { useEffect, useState, useCallback } from "react";

interface MarkdownHelpSheetProps {
  open: boolean;
  onClose: () => void;
}

function Syntax({ children }: { children: React.ReactNode }) {
  return (
    <code className="font-mono text-sm bg-ink/5 px-1.5 py-0.5 rounded">
      {children}
    </code>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs font-semibold uppercase tracking-wider text-ink/50 mt-4 mb-2 first:mt-0">
      {children}
    </h3>
  );
}

function Row({ syntax, result }: { syntax: React.ReactNode; result: string }) {
  return (
    <div className="flex items-baseline gap-4 py-1.5">
      <div className="w-1/2 shrink-0">{typeof syntax === "string" ? <Syntax>{syntax}</Syntax> : syntax}</div>
      <div className="w-1/2 text-sm text-ink/70">{result}</div>
    </div>
  );
}

export function MarkdownHelpSheet({ open, onClose }: MarkdownHelpSheetProps) {
  const [isMac, setIsMac] = useState(false);
  const [visible, setVisible] = useState(false);
  const [animating, setAnimating] = useState(false);

  useEffect(() => {
    setIsMac(/(Mac|iPhone|iPod|iPad)/i.test(navigator.userAgent));
  }, []);

  useEffect(() => {
    if (open) {
      setVisible(true);
      requestAnimationFrame(() => requestAnimationFrame(() => setAnimating(true)));
    } else {
      setAnimating(false);
      const t = setTimeout(() => setVisible(false), 200);
      return () => clearTimeout(t);
    }
  }, [open]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose]
  );

  useEffect(() => {
    if (open) {
      document.addEventListener("keydown", handleKeyDown);
      return () => document.removeEventListener("keydown", handleKeyDown);
    }
  }, [open, handleKeyDown]);

  if (!visible) return null;

  const mod = isMac ? "⌘" : "Ctrl";

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center">
      {/* Backdrop */}
      <div
        className={`absolute inset-0 bg-black/30 transition-opacity duration-200 ${animating ? "opacity-100" : "opacity-0"}`}
        onClick={onClose}
      />

      {/* Panel */}
      <div
        className={`relative bg-white w-full max-h-[70dvh] rounded-t-2xl sm:max-w-lg sm:mx-auto sm:rounded-2xl sm:max-h-[80vh] flex flex-col transition-transform duration-200 ease-out ${animating ? "translate-y-0" : "translate-y-full sm:translate-y-8"}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-ink/10 shrink-0">
          <h2 className="text-base font-semibold text-ink">Markdown Guide</h2>
          <button
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-full hover:bg-ink/5 text-ink/50 hover:text-ink transition-colors"
            aria-label="Close"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="overflow-y-auto px-5 py-3 pb-8">
          <SectionHeader>Text Formatting</SectionHeader>
          <Row syntax="**bold**" result="bold" />
          <Row syntax="*italic*" result="italic" />
          <Row syntax="~~strikethrough~~" result="strikethrough" />
          <Row syntax="`code`" result="inline code" />

          <SectionHeader>Headings</SectionHeader>
          <Row syntax="## Heading 2" result="Large heading" />
          <Row syntax="### Heading 3" result="Medium heading" />

          <SectionHeader>Lists</SectionHeader>
          <Row syntax="- item" result="Bullet list" />
          <Row syntax="1. item" result="Numbered list" />
          <Row syntax="- [ ] task" result="Unchecked checkbox" />
          <Row syntax="- [x] task" result="Checked checkbox" />

          <SectionHeader>Other</SectionHeader>
          <Row syntax="[text](url)" result="Link" />
          <Row syntax="> quote" result="Block quote" />
          <Row syntax="---" result="Horizontal rule" />
          <Row syntax="```" result="Code block" />

          <SectionHeader>Keyboard Shortcuts</SectionHeader>
          <Row syntax={<Syntax>{mod} + B</Syntax>} result="Bold" />
          <Row syntax={<Syntax>{mod} + I</Syntax>} result="Italic" />
          <Row syntax={<Syntax>{mod} + K</Syntax>} result="Insert link" />
          <Row syntax={<Syntax>{mod} + S</Syntax>} result="Save" />
          <Row syntax={<Syntax>Tab</Syntax>} result="Indent list" />
          <Row syntax={<Syntax>Shift + Tab</Syntax>} result="Outdent list" />
        </div>
      </div>
    </div>
  );
}
