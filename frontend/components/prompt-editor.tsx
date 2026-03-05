"use client";

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
} from "react";
import { useEditor, EditorContent, NodeViewWrapper, ReactNodeViewRenderer } from "@tiptap/react";
import { Node, mergeAttributes, type Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { SERVICE_ICONS } from "@/components/service-icon";

/* ------------------------------------------------------------------ */
/*  Chip tag registry — maps known tag names to icons                  */
/* ------------------------------------------------------------------ */

interface ChipTagMeta {
  icon: string;
  iconUrl?: string;
}

const CHIP_TAGS: Record<string, ChipTagMeta> = {
  Google: { icon: "🔗", iconUrl: SERVICE_ICONS["google"] },
  Reddit: { icon: "🔴", iconUrl: SERVICE_ICONS["reddit"] },
  "Daily Journal": { icon: "📝" },
  "Weekly Review": { icon: "📊" },
  PKM: { icon: "🧠" },
  "Web Search": { icon: "🌐" },
  Weather: { icon: "🌤️" },
  News: { icon: "📰" },
  Memory: { icon: "💡" },
};

/* ------------------------------------------------------------------ */
/*  ChipNodeView — React component rendered per chip                   */
/* ------------------------------------------------------------------ */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function ChipNodeView({ node }: { node: any }) {
  const tag = node.attrs.tag as string;
  const meta = CHIP_TAGS[tag];

  return (
    <NodeViewWrapper as="span" className="inline">
      <span
        className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent align-baseline select-none"
        contentEditable={false}
      >
        {meta?.iconUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={meta.iconUrl}
            alt=""
            className="h-3.5 w-3.5 rounded-sm object-contain"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <span className="text-xs leading-none">{meta?.icon ?? "🔖"}</span>
        )}
        {tag}
      </span>
    </NodeViewWrapper>
  );
}

/* ------------------------------------------------------------------ */
/*  ChipNode TipTap extension                                          */
/* ------------------------------------------------------------------ */

const ChipNode = Node.create({
  name: "chip",
  group: "inline",
  inline: true,
  atom: true,

  addAttributes() {
    return {
      tag: { default: "" },
    };
  },

  parseHTML() {
    return [{ tag: 'span[data-chip-tag]', getAttrs: (el) => ({ tag: (el as HTMLElement).getAttribute("data-chip-tag") }) }];
  },

  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes({ "data-chip-tag": HTMLAttributes.tag }, HTMLAttributes), 0];
  },

  addNodeView() {
    return ReactNodeViewRenderer(ChipNodeView);
  },
});

/* ------------------------------------------------------------------ */
/*  Serialization: text ↔ TipTap doc                                   */
/* ------------------------------------------------------------------ */

const TAG_REGEX = /\[([^\]]+)\]/g;

function textToDoc(text: string, editor: Editor) {
  // Split text into lines, then parse each line into paragraph content
  const lines = text.split("\n");
  const content: Array<Record<string, unknown>> = [];

  for (const line of lines) {
    const nodes: Array<Record<string, unknown>> = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;
    const re = new RegExp(TAG_REGEX.source, "g");

    while ((match = re.exec(line)) !== null) {
      // Text before this tag
      if (match.index > lastIndex) {
        nodes.push({ type: "text", text: line.slice(lastIndex, match.index) });
      }
      nodes.push({ type: "chip", attrs: { tag: match[1] } });
      lastIndex = re.lastIndex;
    }

    // Remaining text after last tag
    if (lastIndex < line.length) {
      nodes.push({ type: "text", text: line.slice(lastIndex) });
    }

    content.push({
      type: "paragraph",
      content: nodes.length > 0 ? nodes : undefined,
    });
  }

  editor.commands.setContent({ type: "doc", content });
}

function docToText(editor: Editor): string {
  const json = editor.getJSON();
  if (!json.content) return "";

  return json.content
    .map((block) => {
      if (!block.content) return "";
      return block.content
        .map((node: Record<string, unknown>) => {
          if (node.type === "chip") return `[${(node.attrs as Record<string, string>)?.tag ?? ""}]`;
          if (node.type === "text") return (node.text as string) ?? "";
          if (node.type === "hardBreak") return "\n";
          return "";
        })
        .join("");
    })
    .join("\n");
}

/* ------------------------------------------------------------------ */
/*  PromptEditor component                                             */
/* ------------------------------------------------------------------ */

export interface PromptEditorHandle {
  insertChip: (tag: string) => void;
  focus: () => void;
}

interface PromptEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  minHeight?: string;
}

export const PromptEditor = forwardRef<PromptEditorHandle, PromptEditorProps>(
  function PromptEditor({ value, onChange, placeholder, className, minHeight = "120px" }, ref) {
    const internalChange = useRef(false);

    const editor = useEditor({
      extensions: [
        StarterKit.configure({
          bold: false,
          italic: false,
          strike: false,
          code: false,
          codeBlock: false,
          heading: false,
          blockquote: false,
          bulletList: false,
          orderedList: false,
          listItem: false,
          horizontalRule: false,
        }),
        ChipNode,
        Placeholder.configure({ placeholder: placeholder ?? "" }),
      ],
      content: "",
      editorProps: {
        attributes: {
          class: "prompt-editor-content outline-none w-full px-3 py-2.5 text-sm leading-relaxed text-ink bg-surface",
          style: `min-height: ${minHeight}`,
        },
        handlePaste(view, event) {
          const text = event.clipboardData?.getData("text/plain");
          if (text && TAG_REGEX.test(text)) {
            event.preventDefault();
            // Parse pasted text and insert nodes
            const nodes: Array<Record<string, unknown>> = [];
            let lastIndex = 0;
            let match: RegExpExecArray | null;
            const re = new RegExp(TAG_REGEX.source, "g");
            while ((match = re.exec(text)) !== null) {
              if (match.index > lastIndex) {
                nodes.push({ type: "text", text: text.slice(lastIndex, match.index) });
              }
              nodes.push({ type: "chip", attrs: { tag: match[1] } });
              lastIndex = re.lastIndex;
            }
            if (lastIndex < text.length) {
              nodes.push({ type: "text", text: text.slice(lastIndex) });
            }
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const ed = (view as any).editor as Editor | undefined;
            if (ed) {
              ed.chain().focus().insertContent(nodes).run();
            }
            return true;
          }
          return false;
        },
      },
      onCreate({ editor: ed }) {
        if (value) {
          textToDoc(value, ed);
        }
      },
      onUpdate({ editor: ed }) {
        internalChange.current = true;
        onChange(docToText(ed));
      },
    });

    // Sync external value changes into editor (e.g. template applied)
    useEffect(() => {
      if (!editor) return;
      if (internalChange.current) {
        internalChange.current = false;
        return;
      }
      const current = docToText(editor);
      if (current !== value) {
        textToDoc(value, editor);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [value]);

    // Expose imperative handle
    useImperativeHandle(
      ref,
      () => ({
        insertChip(tag: string) {
          if (!editor) return;
          const tagStr = `[${tag}]`;
          const currentText = docToText(editor);

          // Toggle off if already present
          if (currentText.includes(tagStr)) {
            const newText = currentText.replaceAll(tagStr, "").replace(/ {2,}/g, " ").trim();
            internalChange.current = true;
            textToDoc(newText, editor);
            onChange(newText);
            return;
          }

          // Insert chip at current cursor position
          editor.chain().focus().insertContent({ type: "chip", attrs: { tag } }).insertContent(" ").run();
        },
        focus() {
          editor?.commands.focus();
        },
      }),
      [editor, onChange],
    );

    return (
      <>
        <style>{`
          .prompt-editor-content p { margin: 0; }
          .prompt-editor-content p.is-editor-empty:first-child::before {
            color: var(--color-ink-faint);
            content: attr(data-placeholder);
            float: left;
            height: 0;
            pointer-events: none;
          }
        `}</style>
        <div className={`rounded-panel border border-border overflow-hidden ${className ?? ""}`}>
          <EditorContent
            editor={editor}
            className="overflow-y-auto"
            style={{ maxHeight: "50vh" }}
          />
        </div>
      </>
    );
  },
);
