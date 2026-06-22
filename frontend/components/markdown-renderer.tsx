"use client";

import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { memo, useMemo, useRef } from "react";
import type { Components } from "react-markdown";

interface MarkdownRendererProps {
  content: string;
  onCheckboxToggle?: (lineIndex: number, checked: boolean) => void;
}

// Hoisted: a fresh plugins array per render defeats react-markdown's internal
// memoization (it re-runs the full remark pipeline when the reference changes).
const REMARK_PLUGINS = [remarkGfm, remarkBreaks];

// Matches a GFM task-list item line ("- [ ]", "* [x]", "1. [ ]", indented),
// outside fenced code. Mirrors how remark-gfm recognizes task checkboxes so the
// indices line up 1:1 with the rendered <input>s.
const TASK_LINE_RE = /^\s*(?:[-*+]|\d+[.)])\s+\[[ xX]\]/;
const FENCE_RE = /^\s*(?:```|~~~)/;

// memo'd so parents that re-render on every keystroke (e.g. DocumentView while
// the editor is open) don't re-run the markdown pipeline when `content` and
// `onCheckboxToggle` are unchanged.
export const MarkdownRenderer = memo(function MarkdownRenderer({
  content,
  onCheckboxToggle,
}: MarkdownRendererProps) {
  // 0-based source-line index of every task-list checkbox, in document order.
  // We CANNOT use the AST node's position: remark-gfm emits a synthetic <input>
  // node with no `position` (only the enclosing <li> carries one), so reading
  // node.position.start.line yields undefined for every checkbox. Instead we
  // map the Nth rendered checkbox to the Nth task-list line in the source —
  // react-markdown renders the inputs in document order, so the ordinals align.
  const taskLineIndices = useMemo(() => {
    const out: number[] = [];
    let inFence = false;
    const lines = content.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (FENCE_RE.test(lines[i])) {
        inFence = !inFence;
        continue;
      }
      if (!inFence && TASK_LINE_RE.test(lines[i])) {
        out.push(i);
      }
    }
    return out;
  }, [content]);

  // Reset before each render's children render. react-markdown renders the
  // checkbox <input>s synchronously and in document order during this render,
  // each one consuming the next ordinal.
  const checkboxOrdinal = useRef(0);
  checkboxOrdinal.current = 0;

  const components = useMemo<Components>(
    () => ({
      input: (props: React.InputHTMLAttributes<HTMLInputElement>) => {
        if (props.type !== "checkbox") return <input {...props} />;

        // Tie this checkbox to its source line by document-order position, so
        // clicking checkbox B toggles B — not the first same-state checkbox.
        const sourceLine = taskLineIndices[checkboxOrdinal.current++];

        const handleChange = () => {
          if (!onCheckboxToggle || sourceLine == null) return;
          onCheckboxToggle(sourceLine, !(props.checked ?? false));
        };

        return (
          <input
            {...props}
            disabled={!onCheckboxToggle}
            onChange={handleChange}
            className="mr-2 h-4 w-4 cursor-pointer rounded border-border accent-accent"
          />
        );
      },
    }),
    [taskLineIndices, onCheckboxToggle],
  );

  return (
    <div className="prose prose-sm max-w-none break-words text-ink-muted prose-headings:text-ink prose-h1:text-xl prose-h2:text-lg prose-h3:text-base prose-strong:text-ink prose-a:text-accent prose-li:my-0.5 prose-ul:my-1 prose-ol:my-1 prose-p:my-2 prose-hr:my-4 [&_*]:max-w-full [&_pre]:overflow-x-auto [&_table]:block [&_table]:overflow-x-auto">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
