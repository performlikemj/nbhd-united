"use client";

import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { memo, useMemo } from "react";
import type { Components } from "react-markdown";
import type { Element } from "hast";

interface MarkdownRendererProps {
  content: string;
  onCheckboxToggle?: (lineIndex: number, checked: boolean) => void;
}

// Hoisted: a fresh plugins array per render defeats react-markdown's internal
// memoization (it re-runs the full remark pipeline when the reference changes).
const REMARK_PLUGINS = [remarkGfm, remarkBreaks];

// memo'd so parents that re-render on every keystroke (e.g. DocumentView while
// the editor is open) don't re-run the markdown pipeline when `content` and
// `onCheckboxToggle` are unchanged.
export const MarkdownRenderer = memo(function MarkdownRenderer({
  content,
  onCheckboxToggle,
}: MarkdownRendererProps) {
  const components = useMemo<Components>(
    () => ({
      input: ({
        node,
        ...props
      }: React.InputHTMLAttributes<HTMLInputElement> & {
        node?: Element;
      }) => {
        if (props.type !== "checkbox") return <input {...props} />;

        // Resolve the exact source line this checkbox came from via the AST
        // node's position data, so clicking checkbox B toggles B — not the
        // first same-state checkbox. (react-markdown line numbers are 1-based;
        // onCheckboxToggle expects a 0-based line index.)
        const sourceLine = node?.position?.start.line;

        const handleChange = () => {
          if (!onCheckboxToggle || sourceLine == null) return;
          const checked = props.checked ?? false;
          onCheckboxToggle(sourceLine - 1, !checked);
        };

        return (
          <input
            {...props}
            disabled={!onCheckboxToggle || sourceLine == null}
            onChange={handleChange}
            className="mr-2 h-4 w-4 cursor-pointer rounded border-border accent-accent"
          />
        );
      },
    }),
    [onCheckboxToggle],
  );

  return (
    <div className="prose prose-sm max-w-none break-words text-ink-muted prose-headings:text-ink prose-h1:text-xl prose-h2:text-lg prose-h3:text-base prose-strong:text-ink prose-a:text-accent prose-li:my-0.5 prose-ul:my-1 prose-ol:my-1 prose-p:my-2 prose-hr:my-4 [&_*]:max-w-full [&_pre]:overflow-x-auto [&_table]:block [&_table]:overflow-x-auto">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
