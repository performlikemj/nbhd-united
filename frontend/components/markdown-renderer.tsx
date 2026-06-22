"use client";

import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Children, cloneElement, isValidElement, memo, useMemo } from "react";
import type { ReactElement, ReactNode } from "react";
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
      // Wire interactive task-list checkboxes by overriding the <li>, NOT the
      // <input>. remark-gfm emits a SYNTHETIC checkbox <input> node that has no
      // source position, so neither node.position nor a regex/ordinal scan can
      // reliably map a checkbox back to its markdown line. The enclosing <li>
      // node, however, DOES carry position.start.line — which is exactly the
      // line DocumentView.handleCheckboxToggle rewrites ([ ] <-> [x]). We read
      // the line from the li and clone its checkbox child to attach the toggle,
      // so clicking checkbox B always toggles B regardless of empty tasks,
      // blockquoted tasks, indented text, or same-state runs.
      li: ({
        node,
        children,
        ...props
      }: {
        node?: Element;
        children?: ReactNode;
      } & React.LiHTMLAttributes<HTMLLIElement>) => {
        const line = node?.position?.start?.line;
        if (!onCheckboxToggle || line == null) {
          return <li {...props}>{children}</li>;
        }
        // markdown lines are 1-based; handleCheckboxToggle expects 0-based.
        const sourceLine = line - 1;
        const kids = Children.map(children, (child) => {
          if (
            isValidElement(child) &&
            (child.props as React.InputHTMLAttributes<HTMLInputElement>).type ===
              "checkbox"
          ) {
            const cb = child as ReactElement<
              React.InputHTMLAttributes<HTMLInputElement>
            >;
            const checked = cb.props.checked ?? false;
            return cloneElement(cb, {
              disabled: false,
              onChange: () => onCheckboxToggle(sourceLine, !checked),
              className:
                "mr-2 h-4 w-4 cursor-pointer rounded border-border accent-accent",
            });
          }
          return child;
        });
        return <li {...props}>{kids}</li>;
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
