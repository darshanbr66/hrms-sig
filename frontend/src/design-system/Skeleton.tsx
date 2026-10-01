/** A placeholder shaped like the content while it loads. */
export function Skeleton({ lines = 3, label }: { lines?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2">
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }, (_, index) => (
        <div key={index} aria-hidden className="h-4 animate-pulse rounded bg-neutral-200" />
      ))}
    </div>
  );
}
