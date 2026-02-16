"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useCallback } from "react";
import type { Components } from "react-markdown";

interface MarkdownRendererProps {
  content: string;
  onCheckboxToggle?: (lineIndex: number, checked: boolean) => void;
}

export function MarkdownRenderer({ content, onCheckboxToggle }: MarkdownRendererProps) {
  const components: Components = {
    input: useCallback(
      (props: React.InputHTMLAttributes<HTMLInputElement>) => {
        if (props.type !== "checkbox") return <input {...props} />;

        const handleChange = () => {
          if (!onCheckboxToggle) return;
          // Find which checkbox this is by counting checkboxes in the markdown
          const lines = content.split("\n");
          let checkboxIndex = 0;
          // We need to find the source position. React-markdown doesn't give us this directly,
          // so we use a simpler approach: find nth checkbox
          const checked = props.checked ?? false;

          // Count through lines to find matching checkbox
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const match = line.match(/^(\s*[-*+]\s*)\[([ xX])\]/);
            if (match) {
              const isChecked = match[2] !== " ";
              if (isChecked === checked) {
                // This could be our checkbox. We use the data-sourcepos if available.
                onCheckboxToggle(i, !checked);
                return;
              }
              checkboxIndex++;
            }
          }
        };

        return (
          <input
            {...props}
            disabled={!onCheckboxToggle}
            onChange={handleChange}
            className="mr-2 h-4 w-4 cursor-pointer rounded border-ink/30 accent-accent"
          />
        );
      },
      [content, onCheckboxToggle],
    ),
  };

  return (
    <div className="prose prose-sm max-w-none text-ink/80 prose-headings:text-ink prose-h1:text-xl prose-h2:text-lg prose-h3:text-base prose-strong:text-ink prose-a:text-accent prose-li:my-0.5 prose-ul:my-1 prose-ol:my-1 prose-p:my-2 prose-hr:my-4">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
