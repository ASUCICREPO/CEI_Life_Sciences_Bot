import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { isHttpsUrl } from './util';

// ADR: allow "#fragment" links in addition to https URLs | remark-gfm footnotes
// render internal anchor links (#fn-1, #fnref-1) that must not be stripped.
function isSafeMarkdownUrl(value: unknown): value is string {
  if (typeof value === 'string' && value.startsWith('#')) return true;
  return isHttpsUrl(value);
}

export function SafeMarkdown({ content, className = '' }: { content: string; className?: string }) {
  return (
    <div className={`cei-prose text-sm leading-relaxed text-text-primary [&_p]:mb-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_a]:text-cei-blue [&_a]:underline [&_strong]:font-semibold [&_table]:w-full [&_table]:text-xs [&_th]:text-left [&_th]:border-b [&_th]:border-border-subtle [&_td]:border-b [&_td]:border-border-subtle [&_code]:bg-surface-panel [&_code]:px-1 [&_code]:rounded ${className}`}>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => (isSafeMarkdownUrl(url) ? url : '')}
        components={{
          a: ({ href, children }) => {
            if (!isSafeMarkdownUrl(href)) return <span>{children}</span>;
            const isFragment = href.startsWith('#');
            return (
              <a href={href} target={isFragment ? undefined : '_blank'} rel={isFragment ? undefined : 'noopener noreferrer'}>
                {children}
              </a>
            );
          },
          img: ({ src, alt }) => (isHttpsUrl(src)
            ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || 'Open linked image'}</a>
            : <span>{alt}</span>),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
