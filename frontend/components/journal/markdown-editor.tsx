"use client";

import { useEffect } from "react";
import { useEditor, EditorContent, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { TaskList } from "@tiptap/extension-list";
import { TaskItem } from "@tiptap/extension-list";
import { Markdown } from "tiptap-markdown";
import clsx from "clsx";

// ────────────────────────────────────────────────────────────────────────────
// Props interface — must stay identical to original
// ────────────────────────────────────────────────────────────────────────────

interface MarkdownEditorProps {
  value: string;
  onChange: (value: string) => void;
  onSave?: () => void;
  onHelpToggle?: () => void;
  autoFocus?: boolean;
  minRows?: number;
  className?: string;
  /** localStorage key used to save/restore cursor position across open/close */
  cursorKey?: string;
  /** Suppress the built-in toolbar — use <EditorToolbar> separately (e.g. pinned above keyboard on mobile) */
  hideToolbar?: boolean;
  /** Called with the Tiptap editor instance once ready — use to wire up an external <EditorToolbar> */
  onEditorReady?: (editor: Editor) => void;
}

// ── Inline SVG Icons (16×16, thin stroke) ──────────────────────────────────

const BoldIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M6 4h8a4 4 0 014 4 4 4 0 01-4 4H6V4zM6 12h9a4 4 0 014 4 4 4 0 01-4 4H6v-8z" />
  </svg>
);

const ItalicIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M19 4h-9M14 20H5M15 4L9 20" />
  </svg>
);

const HeadingIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 12h8M4 18V6M12 18V6M17 12l3-3m0 6l-3-3" />
  </svg>
);

const ListIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
    <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
  </svg>
);

const CheckboxIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
  </svg>
);

const IndentIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="3" y1="6" x2="21" y2="6" /><line x1="7" y1="12" x2="21" y2="12" /><line x1="7" y1="18" x2="21" y2="18" />
    <path d="M3 12l4-4v8l-4-4z" />
  </svg>
);

const OutdentIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="3" y1="6" x2="21" y2="6" /><line x1="7" y1="12" x2="21" y2="12" /><line x1="7" y1="18" x2="21" y2="18" />
    <path d="M11 12l-4 4V8l4 4z" />
  </svg>
);

// ── Toolbar ───────────────────────────────────────────────────────────────────

interface ToolbarButtonProps {
  onClick: () => void;
  title: string;
  active?: boolean;
  children: React.ReactNode;
}

function ToolbarButton({ onClick, title, active, children }: ToolbarButtonProps) {
  return (
    <button
      type="button"
      onMouseDown={(e) => {
        e.preventDefault();
        onClick();
      }}
      title={title}
      className={clsx(
        "flex items-center justify-center rounded-lg transition-colors h-9 w-9",
        active
          ? "bg-accent/[0.12] text-accent"
          : "text-ink-faint/60 hover:bg-white/[0.04] hover:text-ink-muted",
      )}
    >
      {children}
    </button>
  );
}

export interface EditorToolbarProps {
  editor: Editor | null;
  className?: string;
}

export function EditorToolbar({ editor, className }: EditorToolbarProps) {
  return (
    <div className={clsx("flex items-center gap-0.5 px-2 py-1.5 overflow-x-auto flex-nowrap scrollbar-none", className)}>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleBold().run()} title="Bold (⌘B)" active={editor?.isActive("bold")}>
        <BoldIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleItalic().run()} title="Italic (⌘I)" active={editor?.isActive("italic")}>
        <ItalicIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()} title="Heading" active={editor?.isActive("heading", { level: 2 })}>
        <HeadingIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleBulletList().run()} title="Bullet list" active={editor?.isActive("bulletList")}>
        <ListIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleTaskList().run()} title="Task list" active={editor?.isActive("taskList")}>
        <CheckboxIcon />
      </ToolbarButton>

      {/* Separator */}
      <div className="mx-1.5 h-4 w-px bg-white/[0.06] shrink-0" />

      <ToolbarButton
        onClick={() => editor?.chain().focus().sinkListItem("listItem").run()}
        title="Indent (Tab)"
        active={false}
      >
        <IndentIcon />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor?.chain().focus().liftListItem("listItem").run()}
        title="Outdent (Shift+Tab)"
        active={false}
      >
        <OutdentIcon />
      </ToolbarButton>
    </div>
  );
}

// ── Component ────────────────────────────────────────────────────────────────

export function MarkdownEditor({
  value,
  onChange,
  onSave,
  onHelpToggle: _onHelpToggle,
  autoFocus,
  className,
  cursorKey,
  hideToolbar,
  onEditorReady,
}: MarkdownEditorProps) {
  const editor = useEditor({
    immediatelyRender: false,
    extensions: [
      StarterKit,
      TaskList,
      TaskItem.configure({ nested: true }),
      Markdown.configure({
        html: false,
        transformCopiedText: true,
        transformPastedText: true,
      }),
    ],
    content: value,
    autofocus: false,
    onCreate({ editor: ed }) {
      onEditorReady?.(ed);
      if (!autoFocus) return;
      const saved = cursorKey ? localStorage.getItem(cursorKey) : null;
      if (saved) {
        const pos = parseInt(saved, 10);
        const clamped = Math.max(0, Math.min(pos, ed.state.doc.content.size - 1));
        ed.commands.setTextSelection(clamped);
      } else {
        ed.commands.setTextSelection(0);
      }
      ed.commands.focus();
    },
    onBlur({ editor: ed }) {
      if (cursorKey) {
        localStorage.setItem(cursorKey, String(ed.state.selection.from));
      }
    },
    editorProps: {
      attributes: {
        class:
          "tiptap-content outline-none min-h-[50vh] w-full px-4 py-3 text-sm leading-relaxed text-ink bg-transparent",
      },
      handleKeyDown(view, event) {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") {
          event.preventDefault();
          onSave?.();
          return true;
        }
        if (event.key === "Tab") {
          event.preventDefault();
          const tiptap = (view as any).editor as Editor | undefined;
          if (event.shiftKey) {
            return tiptap?.chain().focus().liftListItem("listItem").run() ?? false;
          } else {
            return tiptap?.chain().focus().sinkListItem("listItem").run() ?? false;
          }
        }
        return false;
      },
    },
    onUpdate({ editor: ed }) {
      const storage = ed.storage as unknown as { markdown: { getMarkdown: () => string } };
      onChange(storage.markdown.getMarkdown());
    },
  });

  useEffect(() => {
    if (!editor) return;
    const storage = editor.storage as unknown as { markdown: { getMarkdown: () => string } };
    const current = storage.markdown.getMarkdown();
    if (current !== value) {
      editor.commands.setContent(value);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return (
    <>
      {/* Scoped ProseMirror styles — premium writing surface */}
      <style>{`
        .tiptap-content { caret-color: var(--accent); }
        .tiptap-content h1 {
          font-family: var(--font-display), Georgia, serif;
          font-size: 1.35rem;
          font-weight: 700;
          margin-top: 1.25rem;
          margin-bottom: 0.5rem;
          color: var(--color-ink);
          line-height: 1.35;
          letter-spacing: -0.01em;
          position: relative;
        }
        .tiptap-content h1::after {
          content: '';
          display: block;
          width: 40px;
          height: 1px;
          background: linear-gradient(90deg, var(--accent), transparent);
          margin-top: 0.35rem;
          opacity: 0.4;
        }
        .tiptap-content h2 {
          font-family: var(--font-display), Georgia, serif;
          font-size: 1.15rem;
          font-weight: 600;
          margin-top: 1rem;
          margin-bottom: 0.4rem;
          color: var(--color-ink);
          line-height: 1.4;
        }
        .tiptap-content h3 {
          font-size: 1rem;
          font-weight: 600;
          margin-top: 0.875rem;
          margin-bottom: 0.3rem;
          color: var(--color-ink);
          line-height: 1.4;
        }
        .tiptap-content p {
          margin-top: 0.625rem;
          margin-bottom: 0.625rem;
          line-height: 1.75;
          font-size: 0.9375rem;
        }
        .tiptap-content strong { font-weight: 700; color: var(--color-ink); }
        .tiptap-content em { font-style: italic; color: var(--color-ink-muted); }
        .tiptap-content ul { list-style-type: disc; padding-left: 1.5rem; margin-top: 0.375rem; margin-bottom: 0.375rem; }
        .tiptap-content ol { list-style-type: decimal; padding-left: 1.5rem; margin-top: 0.375rem; margin-bottom: 0.375rem; }
        .tiptap-content li { margin-top: 0.25rem; margin-bottom: 0.25rem; line-height: 1.7; }
        .tiptap-content ul ul { list-style-type: circle; padding-left: 1.5rem; }
        .tiptap-content ul ul ul { list-style-type: square; padding-left: 1.5rem; }
        .tiptap-content ol ol { list-style-type: lower-alpha; padding-left: 1.5rem; }
        .tiptap-content ol ol ol { list-style-type: lower-roman; padding-left: 1.5rem; }
        .tiptap-content ul[data-type="taskList"] { list-style: none; padding-left: 0.375rem; }
        .tiptap-content ul[data-type="taskList"] li { display: flex; align-items: flex-start; gap: 0.5rem; }
        .tiptap-content ul[data-type="taskList"] li > label { margin-top: 0.15rem; }
        .tiptap-content ul[data-type="taskList"] li > label input[type="checkbox"] {
          accent-color: var(--color-accent);
          width: 1rem; height: 1rem; cursor: pointer;
          border-radius: 0.25rem;
        }
        .tiptap-content code {
          font-family: var(--font-mono), ui-monospace, monospace;
          background-color: rgba(124, 107, 240, 0.08);
          border-radius: 0.35rem;
          padding: 0.1rem 0.35rem;
          font-size: 0.82em;
          color: var(--color-accent-hover);
        }
        .tiptap-content pre {
          background-color: rgba(0, 0, 0, 0.25);
          border-radius: 0.5rem;
          padding: 0.875rem 1.125rem;
          overflow-x: auto;
          margin: 0.75rem 0;
          border: 1px solid rgba(255,255,255,0.04);
        }
        .tiptap-content pre code { background: none; padding: 0; }
        .tiptap-content blockquote {
          border-left: 2px solid var(--accent);
          padding-left: 1rem;
          color: var(--color-ink-faint);
          margin: 0.75rem 0;
          font-style: italic;
        }
        .tiptap-content hr {
          border: none;
          height: 1px;
          background: linear-gradient(90deg, transparent, rgba(124, 107, 240, 0.2), transparent);
          margin: 1.25rem 0;
        }
        .tiptap-content p.is-editor-empty:first-child::before {
          color: var(--color-ink-faint);
          content: attr(data-placeholder);
          float: left;
          height: 0;
          pointer-events: none;
          font-style: italic;
        }
      `}</style>

      <div className={clsx("overflow-hidden", className)}>
        {/* Toolbar */}
        {!hideToolbar && (
          <div className="sticky top-0 z-10 border-b border-white/[0.04] bg-white/[0.015] backdrop-blur-sm rounded-t-lg">
            <EditorToolbar editor={editor} />
          </div>
        )}

        {/* Tiptap editor content */}
        <EditorContent editor={editor} className="overflow-y-auto" />
      </div>
    </>
  );
}
