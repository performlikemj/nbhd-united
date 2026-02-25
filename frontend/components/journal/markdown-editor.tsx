"use client";

import { useEffect } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
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
}

// ── Inline SVG Icons (16×16) ─────────────────────────────────────────────────

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

// ── Toolbar button ────────────────────────────────────────────────────────────

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
        // Prevent editor losing focus on toolbar click
        e.preventDefault();
        onClick();
      }}
      title={title}
      className={clsx(
        "flex items-center justify-center rounded transition-colors min-h-[44px] min-w-[44px] p-1.5",
        active
          ? "bg-accent/15 text-accent"
          : "text-ink-faint hover:bg-border hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

// ── Component ────────────────────────────────────────────────────────────────

export function MarkdownEditor({
  value,
  onChange,
  onSave,
  // onHelpToggle is kept in the interface but the help button is hidden
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  onHelpToggle: _onHelpToggle,
  autoFocus,
  className,
}: MarkdownEditorProps) {
  const editor = useEditor({
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
    autofocus: autoFocus ? "end" : false,
    editorProps: {
      attributes: {
        class:
          "tiptap-content outline-none min-h-[50vh] w-full px-4 py-3 text-sm leading-relaxed text-ink bg-surface",
      },
      handleKeyDown(_, event) {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") {
          event.preventDefault();
          onSave?.();
          return true;
        }
        return false;
      },
    },
    onUpdate({ editor: ed }) {
      const storage = ed.storage as unknown as { markdown: { getMarkdown: () => string } };
      onChange(storage.markdown.getMarkdown());
    },
  });

  // Sync external value changes into editor (e.g. cancel → reset)
  useEffect(() => {
    if (!editor) return;
    const storage = editor.storage as unknown as { markdown: { getMarkdown: () => string } };
    const current = storage.markdown.getMarkdown();
    if (current !== value) {
      editor.commands.setContent(value);
    }
  // Only run when `value` changes externally (not on every onChange echo)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return (
    <>
      {/* Scoped ProseMirror styles */}
      <style>{`
        .tiptap-content h1 { font-size: 1.25rem; font-weight: 700; margin-top: 1rem; margin-bottom: 0.25rem; color: var(--color-ink); }
        .tiptap-content h2 { font-size: 1.125rem; font-weight: 600; margin-top: 0.875rem; margin-bottom: 0.25rem; color: var(--color-ink); }
        .tiptap-content h3 { font-size: 1rem; font-weight: 600; margin-top: 0.75rem; margin-bottom: 0.125rem; color: var(--color-ink); }
        .tiptap-content p { margin-top: 0.5rem; margin-bottom: 0.5rem; }
        .tiptap-content strong { font-weight: 700; color: var(--color-ink); }
        .tiptap-content em { font-style: italic; }
        .tiptap-content ul { list-style-type: disc; padding-left: 1.25rem; margin-top: 0.25rem; margin-bottom: 0.25rem; }
        .tiptap-content ol { list-style-type: decimal; padding-left: 1.25rem; margin-top: 0.25rem; margin-bottom: 0.25rem; }
        .tiptap-content li { margin-top: 0.125rem; margin-bottom: 0.125rem; }
        .tiptap-content ul[data-type="taskList"] { list-style: none; padding-left: 0.25rem; }
        .tiptap-content ul[data-type="taskList"] li { display: flex; align-items: flex-start; gap: 0.5rem; }
        .tiptap-content ul[data-type="taskList"] li > label { margin-top: 0.125rem; }
        .tiptap-content ul[data-type="taskList"] li > label input[type="checkbox"] {
          accent-color: var(--color-accent);
          width: 1rem; height: 1rem; cursor: pointer;
          border-radius: 0.25rem;
        }
        .tiptap-content code { font-family: ui-monospace, monospace; background-color: var(--color-surface-hover); border-radius: 0.25rem; padding: 0.1rem 0.3rem; font-size: 0.85em; }
        .tiptap-content pre { background-color: var(--color-surface-hover); border-radius: 0.375rem; padding: 0.75rem 1rem; overflow-x: auto; margin: 0.5rem 0; }
        .tiptap-content pre code { background: none; padding: 0; }
        .tiptap-content blockquote { border-left: 3px solid var(--color-border-strong); padding-left: 1rem; color: var(--color-ink-faint); margin: 0.5rem 0; }
        .tiptap-content hr { border-color: var(--color-border); margin: 1rem 0; }
        .tiptap-content p.is-editor-empty:first-child::before {
          color: var(--color-ink-faint); content: attr(data-placeholder); float: left; height: 0; pointer-events: none;
        }
      `}</style>

      <div className={clsx("rounded-lg border border-border overflow-hidden", className)}>
        {/* Toolbar */}
        <div className="flex items-center gap-0.5 px-2 py-1.5 border-b border-border bg-surface-hover overflow-x-auto flex-nowrap">
          <ToolbarButton
            onClick={() => editor?.chain().focus().toggleBold().run()}
            title="Bold (⌘B)"
            active={editor?.isActive("bold")}
          >
            <BoldIcon />
          </ToolbarButton>

          <ToolbarButton
            onClick={() => editor?.chain().focus().toggleItalic().run()}
            title="Italic (⌘I)"
            active={editor?.isActive("italic")}
          >
            <ItalicIcon />
          </ToolbarButton>

          <ToolbarButton
            onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}
            title="Heading 2"
            active={editor?.isActive("heading", { level: 2 })}
          >
            <HeadingIcon />
          </ToolbarButton>

          <ToolbarButton
            onClick={() => editor?.chain().focus().toggleBulletList().run()}
            title="Bullet list"
            active={editor?.isActive("bulletList")}
          >
            <ListIcon />
          </ToolbarButton>

          <ToolbarButton
            onClick={() => editor?.chain().focus().toggleTaskList().run()}
            title="Task list"
            active={editor?.isActive("taskList")}
          >
            <CheckboxIcon />
          </ToolbarButton>
        </div>

        {/* Tiptap editor content */}
        <EditorContent editor={editor} className="overflow-y-auto" />
      </div>
    </>
  );
}
