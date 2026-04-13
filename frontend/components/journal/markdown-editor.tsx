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

const IndentIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M2 3h12v1.5H2V3Zm4 3.5h8V8H6V6.5Zm0 3.5h8v1.5H6V10ZM2 13h12v1.5H2V13ZM2 6l3 2.5L2 11V6Z" />
  </svg>
);

const OutdentIcon = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
    <path d="M2 3h12v1.5H2V3Zm4 3.5h8V8H6V6.5Zm0 3.5h8v1.5H6V10ZM2 13h12v1.5H2V13ZM5 6l-3 2.5L5 11V6Z" />
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

// ── EditorToolbar (exported — use standalone when hideToolbar=true) ──────────

interface EditorToolbarProps {
  editor: Editor | null;
  /** Extra classes — e.g. border-t for bottom placement */
  className?: string;
}

export function EditorToolbar({ editor, className }: EditorToolbarProps) {
  return (
    <div className={clsx("flex items-center gap-0.5 px-2 py-1.5 bg-surface-hover overflow-x-auto flex-nowrap", className)}>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleBold().run()} title="Bold (⌘B)" active={editor?.isActive("bold")}>
        <BoldIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleItalic().run()} title="Italic (⌘I)" active={editor?.isActive("italic")}>
        <ItalicIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()} title="Heading 2" active={editor?.isActive("heading", { level: 2 })}>
        <HeadingIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleBulletList().run()} title="Bullet list" active={editor?.isActive("bulletList")}>
        <ListIcon />
      </ToolbarButton>
      <ToolbarButton onClick={() => editor?.chain().focus().toggleTaskList().run()} title="Task list" active={editor?.isActive("taskList")}>
        <CheckboxIcon />
      </ToolbarButton>

      {/* Separator */}
      <div className="mx-1 h-5 w-px bg-border shrink-0" />

      {/* Indent / Outdent — always visible so mobile users know they exist; dim when not in a list */}
      <ToolbarButton
        onClick={() => editor?.chain().focus().sinkListItem("listItem").run()}
        title="Indent list item (Tab)"
        active={false}
      >
        <IndentIcon />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor?.chain().focus().liftListItem("listItem").run()}
        title="Outdent list item (Shift+Tab)"
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
  // onHelpToggle is kept in the interface but the help button is hidden
   
  onHelpToggle: _onHelpToggle,
  autoFocus,
  className,
  cursorKey,
  hideToolbar,
  onEditorReady,
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
    // Don't use autofocus here — cursor restore is handled in onCreate
    autofocus: false,
    onCreate({ editor: ed }) {
      onEditorReady?.(ed);
      if (!autoFocus) return;
      // Restore last cursor position if available, otherwise go to start
      const saved = cursorKey ? localStorage.getItem(cursorKey) : null;
      if (saved) {
        const pos = parseInt(saved, 10);
        // Clamp to doc size in case content has changed since last visit
        const clamped = Math.max(0, Math.min(pos, ed.state.doc.content.size - 1));
        ed.commands.setTextSelection(clamped);
      } else {
        ed.commands.setTextSelection(0);
      }
      ed.commands.focus();
    },
    onBlur({ editor: ed }) {
      // Save cursor position whenever the editor loses focus
      if (cursorKey) {
        localStorage.setItem(cursorKey, String(ed.state.selection.from));
      }
    },
    editorProps: {
      attributes: {
        class:
          "tiptap-content outline-none min-h-[50vh] w-full px-4 py-3 text-sm leading-relaxed text-ink bg-surface",
      },
      handleKeyDown(view, event) {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") {
          event.preventDefault();
          onSave?.();
          return true;
        }
        // Tab → indent list item; Shift+Tab → outdent
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
        .tiptap-content ul ul { list-style-type: circle; padding-left: 1.25rem; }
        .tiptap-content ul ul ul { list-style-type: square; padding-left: 1.25rem; }
        .tiptap-content ol ol { list-style-type: lower-alpha; padding-left: 1.25rem; }
        .tiptap-content ol ol ol { list-style-type: lower-roman; padding-left: 1.25rem; }
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
        {/* Toolbar — top position (default, desktop) */}
        {!hideToolbar && (
          <EditorToolbar editor={editor} className="border-b border-border" />
        )}

        {/* Tiptap editor content */}
        <EditorContent editor={editor} className="overflow-y-auto" />
      </div>
    </>
  );
}
