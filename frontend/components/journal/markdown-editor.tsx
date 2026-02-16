"use client";

import { useRef, useCallback, useEffect } from "react";
import clsx from "clsx";

interface MarkdownEditorProps {
  value: string;
  onChange: (value: string) => void;
  onSave?: () => void;
  onHelpToggle?: () => void;
  autoFocus?: boolean;
  minRows?: number;
  className?: string;
}

// ── Inline SVG Icons (16×16) ────────────────────────────────────────────────

const BoldIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M4 2h4.5a3 3 0 0 1 2.1 5.15A3.5 3.5 0 0 1 9 14H4V2Zm2 5h2.5a1 1 0 1 0 0-2H6v2Zm0 2v3h3a1.5 1.5 0 0 0 0-3H6Z" />
  </svg>
);

const ItalicIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M6 2h6v2h-2.2l-2.6 8H9v2H3v-2h2.2l2.6-8H6V2Z" />
  </svg>
);

const HeadingIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M3 2v12h2V9h6v5h2V2h-2v5H5V2H3Z" />
  </svg>
);

const ListIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M2 4a1 1 0 1 1 2 0 1 1 0 0 1-2 0Zm4-1h8v2H6V3Zm0 4h8v2H6V7Zm0 4h8v2H6v-2ZM2 8a1 1 0 1 1 2 0 1 1 0 0 1-2 0Zm0 4a1 1 0 1 1 2 0 1 1 0 0 1-2 0Z" />
  </svg>
);

const CheckboxIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
    <rect x="2" y="2" width="12" height="12" rx="2" />
    <path d="M5 8l2 2 4-4" />
  </svg>
);

const LinkIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M6.354 8.354a.5.5 0 0 0 .707 0l3.182-3.182a2.5 2.5 0 0 1 3.536 3.536l-3.182 3.182a.5.5 0 0 0 .707.707l3.182-3.182a3.5 3.5 0 0 0-4.95-4.95L6.354 7.647a.5.5 0 0 0 0 .707Zm3.292-.708a.5.5 0 0 0-.707 0L5.757 10.83a2.5 2.5 0 0 1-3.536-3.536l3.182-3.182a.5.5 0 0 0-.707-.707L1.514 6.586a3.5 3.5 0 0 0 4.95 4.95l3.182-3.183a.5.5 0 0 0 0-.707Z" />
  </svg>
);

const CodeIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M5.854 4.146a.5.5 0 0 1 0 .708L2.707 8l3.147 3.146a.5.5 0 0 1-.708.708l-3.5-3.5a.5.5 0 0 1 0-.708l3.5-3.5a.5.5 0 0 1 .708 0Zm4.292 0a.5.5 0 0 0 0 .708L13.293 8l-3.147 3.146a.5.5 0 0 0 .708.708l3.5-3.5a.5.5 0 0 0 0-.708l-3.5-3.5a.5.5 0 0 0-.708 0Z" />
  </svg>
);

// ── Helpers ──────────────────────────────────────────────────────────────────

function getLineRange(text: string, pos: number) {
  const start = text.lastIndexOf("\n", pos - 1) + 1;
  let end = text.indexOf("\n", pos);
  if (end === -1) end = text.length;
  return { start, end, line: text.slice(start, end) };
}

function setTextAndCursor(
  ta: HTMLTextAreaElement,
  newValue: string,
  cursorPos: number,
  onChange: (v: string) => void,
  selEnd?: number,
) {
  onChange(newValue);
  requestAnimationFrame(() => {
    ta.setSelectionRange(cursorPos, selEnd ?? cursorPos);
    ta.focus();
  });
}

// ── Component ───────────────────────────────────────────────────────────────

export function MarkdownEditor({
  value,
  onChange,
  onSave,
  onHelpToggle,
  autoFocus,
  className,
}: MarkdownEditorProps) {
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (autoFocus) taRef.current?.focus();
  }, [autoFocus]);

  // ── Toolbar helpers ─────────────────────────────────────────────────────

  const wrapSelection = useCallback(
    (before: string, after: string, placeholder?: string) => {
      const ta = taRef.current;
      if (!ta) return;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const sel = value.slice(start, end);
      if (sel) {
        const nv = value.slice(0, start) + before + sel + after + value.slice(end);
        setTextAndCursor(ta, nv, start + before.length, onChange, start + before.length + sel.length);
      } else {
        const ph = placeholder ?? "";
        const nv = value.slice(0, start) + before + ph + after + value.slice(end);
        setTextAndCursor(ta, nv, start + before.length, onChange, start + before.length + ph.length);
      }
    },
    [value, onChange],
  );

  const handleBold = useCallback(() => wrapSelection("**", "**"), [wrapSelection]);
  const handleItalic = useCallback(() => wrapSelection("*", "*"), [wrapSelection]);

  const handleHeading = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    const { start, end, line } = getLineRange(value, ta.selectionStart);
    let newLine: string;
    if (line.startsWith("### ")) {
      newLine = line.slice(4);
    } else if (line.startsWith("## ")) {
      newLine = "### " + line.slice(3);
    } else {
      newLine = "## " + line;
    }
    const nv = value.slice(0, start) + newLine + value.slice(end);
    setTextAndCursor(ta, nv, start + newLine.length, onChange);
  }, [value, onChange]);

  const toggleLinePrefix = useCallback(
    (prefix: string) => {
      const ta = taRef.current;
      if (!ta) return;
      const selStart = ta.selectionStart;
      const selEnd = ta.selectionEnd;
      // find all lines in selection
      const lineStart = value.lastIndexOf("\n", selStart - 1) + 1;
      let lineEnd = value.indexOf("\n", selEnd);
      if (lineEnd === -1) lineEnd = value.length;
      const block = value.slice(lineStart, lineEnd);
      const lines = block.split("\n");
      const allHave = lines.every((l) => l.startsWith(prefix));
      const newLines = allHave
        ? lines.map((l) => l.slice(prefix.length))
        : lines.map((l) => prefix + l);
      const newBlock = newLines.join("\n");
      const nv = value.slice(0, lineStart) + newBlock + value.slice(lineEnd);
      const diff = newBlock.length - block.length;
      setTextAndCursor(ta, nv, selEnd + diff, onChange);
    },
    [value, onChange],
  );

  const handleList = useCallback(() => toggleLinePrefix("- "), [toggleLinePrefix]);
  const handleCheckbox = useCallback(() => toggleLinePrefix("- [ ] "), [toggleLinePrefix]);

  const handleLink = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const sel = value.slice(start, end);
    if (sel) {
      const nv = value.slice(0, start) + "[" + sel + "](url)" + value.slice(end);
      // select "url"
      const urlStart = start + sel.length + 3;
      setTextAndCursor(ta, nv, urlStart, onChange, urlStart + 3);
    } else {
      const nv = value.slice(0, start) + "[text](url)" + value.slice(end);
      const urlStart = start + 7;
      setTextAndCursor(ta, nv, urlStart, onChange, urlStart + 3);
    }
  }, [value, onChange]);

  const handleCode = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const sel = value.slice(start, end);
    if (sel.includes("\n")) {
      const nv = value.slice(0, start) + "```\n" + sel + "\n```" + value.slice(end);
      setTextAndCursor(ta, nv, start + 4, onChange, start + 4 + sel.length);
    } else {
      wrapSelection("`", "`");
    }
  }, [value, onChange, wrapSelection]);

  // ── Keyboard handling ───────────────────────────────────────────────────

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      const ta = taRef.current;
      if (!ta) return;
      const mod = e.metaKey || e.ctrlKey;

      // Shortcuts
      if (mod && e.key === "b") {
        e.preventDefault();
        handleBold();
        return;
      }
      if (mod && e.key === "i") {
        e.preventDefault();
        handleItalic();
        return;
      }
      if (mod && e.key === "k") {
        e.preventDefault();
        handleLink();
        return;
      }
      if (mod && e.key === "s") {
        e.preventDefault();
        onSave?.();
        return;
      }
      if (mod && e.shiftKey && (e.key === "x" || e.key === "X")) {
        e.preventDefault();
        handleCheckbox();
        return;
      }

      // Enter — list continuation
      if (e.key === "Enter" && !mod && !e.shiftKey) {
        const pos = ta.selectionStart;
        const { start, line } = getLineRange(value, pos);
        // empty bullet — exit list
        const emptyBullet = line.match(/^(\s*)([-*+])\s*$/);
        if (emptyBullet) {
          e.preventDefault();
          const nv = value.slice(0, start) + "\n" + value.slice(start + line.length);
          setTextAndCursor(ta, nv, start + 1, onChange);
          return;
        }
        // checkbox line
        const checkMatch = line.match(/^(\s*)([-*+])\s\[[ xX]\]\s(.+)$/);
        if (checkMatch) {
          e.preventDefault();
          const prefix = checkMatch[1] + "- [ ] ";
          const nv = value.slice(0, pos) + "\n" + prefix + value.slice(pos);
          setTextAndCursor(ta, nv, pos + 1 + prefix.length, onChange);
          return;
        }
        // bullet line
        const bulletMatch = line.match(/^(\s*)([-*+])\s(.+)$/);
        if (bulletMatch) {
          e.preventDefault();
          const prefix = bulletMatch[1] + bulletMatch[2] + " ";
          const nv = value.slice(0, pos) + "\n" + prefix + value.slice(pos);
          setTextAndCursor(ta, nv, pos + 1 + prefix.length, onChange);
          return;
        }
        // ordered list
        const ordMatch = line.match(/^(\s*)(\d+)\.\s(.+)$/);
        if (ordMatch) {
          e.preventDefault();
          const num = parseInt(ordMatch[2], 10) + 1;
          const prefix = ordMatch[1] + num + ". ";
          const nv = value.slice(0, pos) + "\n" + prefix + value.slice(pos);
          setTextAndCursor(ta, nv, pos + 1 + prefix.length, onChange);
          return;
        }
      }

      // Tab — indent/outdent
      if (e.key === "Tab") {
        e.preventDefault();
        const selStart = ta.selectionStart;
        const selEnd = ta.selectionEnd;
        const lineStart = value.lastIndexOf("\n", selStart - 1) + 1;
        let lineEnd = value.indexOf("\n", selEnd);
        if (lineEnd === -1) lineEnd = value.length;
        const block = value.slice(lineStart, lineEnd);
        const lines = block.split("\n");

        if (e.shiftKey) {
          // outdent
          const newLines = lines.map((l) => (l.startsWith("  ") ? l.slice(2) : l));
          const newBlock = newLines.join("\n");
          const nv = value.slice(0, lineStart) + newBlock + value.slice(lineEnd);
          const diff = newBlock.length - block.length;
          setTextAndCursor(ta, nv, Math.max(lineStart, selStart + (lines[0].startsWith("  ") ? -2 : 0)), onChange, selEnd + diff);
        } else {
          // indent
          const newLines = lines.map((l) => "  " + l);
          const newBlock = newLines.join("\n");
          const nv = value.slice(0, lineStart) + newBlock + value.slice(lineEnd);
          const diff = newBlock.length - block.length;
          setTextAndCursor(ta, nv, selStart + 2, onChange, selEnd + diff);
        }
      }
    },
    [value, onChange, onSave, handleBold, handleItalic, handleLink, handleCheckbox],
  );

  // ── Toolbar button component ────────────────────────────────────────────

  const btnClass =
    "p-1.5 rounded hover:bg-ink/10 text-ink/50 hover:text-ink min-h-[36px] min-w-[36px] flex items-center justify-center transition-colors";

  return (
    <div className={clsx("rounded-lg border border-ink/15 overflow-hidden", className)}>
      {/* Toolbar */}
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-ink/10 bg-ink/[0.02] overflow-x-auto flex-nowrap">
        <button type="button" className={btnClass} onClick={handleBold} title="Bold (⌘B)">
          <BoldIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleItalic} title="Italic (⌘I)">
          <ItalicIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleHeading} title="Heading">
          <HeadingIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleList} title="Bullet list">
          <ListIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleCheckbox} title="Checkbox (⌘⇧X)">
          <CheckboxIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleLink} title="Link (⌘K)">
          <LinkIcon />
        </button>
        <button type="button" className={btnClass} onClick={handleCode} title="Code">
          <CodeIcon />
        </button>

        {/* Divider */}
        <div className="w-px h-5 bg-ink/10 mx-1 shrink-0" />

        <button
          type="button"
          className={btnClass}
          onClick={onHelpToggle}
          title="Markdown help"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1Zm0 12.5a5.5 5.5 0 1 1 0-11 5.5 5.5 0 0 1 0 11Zm-.75-2.5h1.5v1.5h-1.5V11Zm.75-7a2.5 2.5 0 0 0-2.5 2.5h1.5a1 1 0 1 1 2 0c0 .53-.2.78-.7 1.2l-.15.13C7.55 8.37 7.25 9 7.25 10h1.5c0-.53.2-.78.7-1.2l.15-.13C10.2 8.13 10.5 7.5 10.5 6.5A2.5 2.5 0 0 0 8 4Z" />
          </svg>
        </button>
      </div>

      {/* Textarea */}
      <textarea
        ref={taRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        className="w-full bg-white px-4 py-3 font-mono text-sm leading-relaxed focus:outline-none resize-y min-h-[50dvh] md:min-h-[300px]"
        autoFocus={autoFocus}
      />
    </div>
  );
}
