import { Button } from "./Button";

/** Cursor pagination: the next page is fetched on request, and the end of the list is stated. */
export function LoadMore({
  hasMore,
  isLoading,
  onLoad,
  shown,
}: {
  hasMore: boolean;
  isLoading: boolean;
  onLoad: () => void;
  shown: number;
}) {
  return (
    <div className="mt-4 flex items-center justify-between gap-4 text-sm text-neutral-600">
      <span className="tabular">Showing {shown}</span>
      {hasMore ? (
        <Button variant="secondary" size="sm" onPress={onLoad} isPending={isLoading}>
          Load more
        </Button>
      ) : (
        <span>End of the list</span>
      )}
    </div>
  );
}
