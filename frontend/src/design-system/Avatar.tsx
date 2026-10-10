/** Initials in a circle. Decorative: the name is always written next to it. */
export function Avatar({ name, size = "md" }: { name: string; size?: "sm" | "md" | "lg" }) {
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
  const sizes = { sm: "size-7 text-xs", md: "size-9 text-sm", lg: "size-14 text-lg" };
  return (
    <span
      aria-hidden
      className={`inline-flex shrink-0 items-center justify-center rounded-full bg-accent-100 font-semibold text-accent-800 ${sizes[size]}`}
    >
      {initials || "?"}
    </span>
  );
}
