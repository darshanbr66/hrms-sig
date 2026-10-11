/** Requests waiting for the viewer's decision (product-requirements LV-5): their team's as a
 * manager, or everyone their HR role covers; never their own. Oldest first. */
import { PageHeader } from "../../design-system/PageHeader";
import { RequestTable } from "./RequestTable";

export function ApprovalsPage() {
  return (
    <div>
      <PageHeader
        title="Leave approvals"
        description="Requests and cancellations waiting for your decision, the earliest leave first. Open one to see the reason and decide."
      />
      <RequestTable
        label="Waiting for your decision"
        query={{ awaiting_my_decision: true }}
        showEmployee
        empty={{
          title: "Nothing is waiting for your decision",
          body: "New requests from your team appear here.",
        }}
      />
    </div>
  );
}
