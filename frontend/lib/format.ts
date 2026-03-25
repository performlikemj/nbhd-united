/**
 * Strip markdown formatting from text, returning plain text.
 * Shared across goal-card, weekly-pulse, and other components.
 */
export function stripMarkdown(text: string): string {
  return text
    .replace(/^#{1,6}\s+/gm, "")            // headings
    .replace(/\*\*(.+?)\*\*/g, "$1")         // bold
    .replace(/\*(.+?)\*/g, "$1")             // italic
    .replace(/_(.+?)_/g, "$1")               // italic alt
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1") // links
    .replace(/^[-*]\s+/gm, "")              // list items
    .replace(/^>\s+/gm, "")                 // blockquotes
    .replace(/`([^`]+)`/g, "$1")             // inline code
    .replace(/\[[ x]\]\s*/gi, "")            // checkboxes [ ] and [x]
    .replace(/\n{2,}/g, " ")                 // collapse blank lines
    .replace(/\n/g, " ")                     // remaining newlines
    .trim();
}
