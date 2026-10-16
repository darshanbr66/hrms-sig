/** Attendance corrections for managers and HR: those waiting for the viewer's decision (never
 * their own, SOD-1), and every correction in their scope. Reasons are on each correction's page. */
import { useInfiniteQuery } from "@tanstack/react-query";
import { ClipboardCheck } from "lucide-react";

import { canSee } from "../../app/navigation";
import { useMe } from "../../app/session";
import { EmptyState } from "../../design-system/EmptyState";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { Tabs } from "../../design-system/Tabs";
import { client, unwrap } from "../../lib/api";
import { CorrectionList } from "./MyAttendancePage";
import { ATTENDANCE_KEY } from "./queries";

function Corrections({ forDecision }: { forDecision: boolean }) {
  const list = useInfiniteQuery({
    queryKey: [...ATTENDANCE_KEY, "corrections", forDecision ? "decide" : "scope"],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/attendance/corrections", {
          params: {
            query: { for_decision: forDecision, limit: 50, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = list.data?.pages.flatMap((page) => page.items) ?? [];
  if (list.isPending) return <Skeleton label="Loading corrections" lines={4} />;
  if (list.isError) {
    return (
      <ProblemMessage
        error={list.error}
        fallback="We couldn't load the corrections."
        onRetry={() => void list.refetch()}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={ClipboardCheck}
        title={forDecision ? "Nothing waiting for you" : "No corrections yet"}
      >
        {forDecision
          ? "Corrections your team asks for appear here, oldest first."
          : "Corrections asked for or recorded in your scope appear here."}
      </EmptyState>
    );
  }
  return (
    <>
      <CorrectionList
        label={forDecision ? "Corrections waiting for you" : "Corrections"}
        rows={rows}
        showEmployee
      />
      <LoadMore
        shown={rows.length}
        hasMore={list.hasNextPage}
        isLoading={list.isFetchingNextPage}
        onLoad={() => void list.fetchNextPage()}
      />
    </>
  );
}

export function CorrectionsPage() {
  const me = useMe();
  const canDecide = canSee(me.data, {
    anyOf: ["attendance.correction.approve.team", "attendance.correction.approve.all"],
  });
  return (
    <div>
      <PageHeader
        title="Attendance corrections"
        description="Approving a correction adds the corrected events and keeps the originals visible. Nobody decides their own."
      />
      <Tabs
        label="Attendance corrections"
        items={[
          ...(canDecide
            ? [{ id: "decide", label: "Waiting for me", content: <Corrections forDecision /> }]
            : []),
          { id: "all", label: "All in my scope", content: <Corrections forDecision={false} /> },
        ]}
      />
    </div>
  );
}
