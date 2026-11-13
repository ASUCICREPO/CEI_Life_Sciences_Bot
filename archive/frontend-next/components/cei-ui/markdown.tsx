'use client';

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { isHttpsUrl } from './util';

// ADR: allow "#fragment" links in addition to https URLs | remark-gfm footnotes
// render internal anchor links (#fn-1, #fnref-1) that must not be stripped.
function isSafeMarkdownUrl(value: unknown): value is string {
  if (typeof value === 'string' && value.startsWith('#')) return true;
  return isHttpsUrl(value);
}

export function SafeMarkdown({ content, className = 'cei-markdown' }: { content: string; className?: string }) {
  return (
    <div className={className}>
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
