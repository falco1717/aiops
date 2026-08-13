/**
 * AIOps identity.
 *
 * The mark is an orchestration glyph: one bright core (the operator) wired to
 * three satellite nodes (the agents it dispatches). It stays legible down to
 * favicon size because the nodes sit on a triangle rather than a ring, so they
 * keep their spacing when the strokes get heavy.
 */

let gradientSeq = 0;

export function Mark({ size = 32, className }: { size?: number; className?: string }) {
  // Unique gradient ids so several marks on one page don't collide.
  const uid = `aiops-${(gradientSeq += 1)}`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      className={className}
      role="img"
      aria-label="AIOps"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient id={`${uid}-bg`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#5AA9FF" />
          <stop offset="55%" stopColor="#7B6BFF" />
          <stop offset="100%" stopColor="#A45BFF" />
        </linearGradient>
        <linearGradient id={`${uid}-core`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#EAF4FF" />
          <stop offset="100%" stopColor="#7BE8D8" />
        </linearGradient>
      </defs>

      <rect width="64" height="64" rx="16" fill={`url(#${uid}-bg)`} />

      {/* edges: core -> each satellite */}
      <g stroke="#0E1116" strokeWidth="3.25" strokeLinecap="round" opacity="0.92">
        <path d="M32 32 L32 15.5" />
        <path d="M32 32 L17.4 44.5" />
        <path d="M32 32 L46.6 44.5" />
      </g>

      {/* satellites */}
      <g fill="#0E1116">
        <circle cx="32" cy="14" r="5.6" />
        <circle cx="16.2" cy="46" r="5.6" />
        <circle cx="47.8" cy="46" r="5.6" />
      </g>

      {/* core */}
      <circle cx="32" cy="32" r="8.6" fill="#0E1116" />
      <circle cx="32" cy="32" r="5.2" fill={`url(#${uid}-core)`} />
    </svg>
  );
}

export function Wordmark({ tagline = true }: { tagline?: boolean }) {
  return (
    <span className="wordmark">
      <span className="wordmark-name">
        <span className="wordmark-ai">AI</span>Ops
      </span>
      {tagline && <span className="wordmark-tagline">Make Thing Intelligent</span>}
    </span>
  );
}

export default function Logo({
  size = 34,
  tagline = true,
  className,
}: {
  size?: number;
  tagline?: boolean;
  className?: string;
}) {
  return (
    <span className={`logo${className ? ` ${className}` : ""}`}>
      <Mark size={size} />
      <Wordmark tagline={tagline} />
    </span>
  );
}
