/** The employee's own leave (product-requirements LV-2, LV-6): balances, requests and the
 * ledger, with the way to ask for leave. */
import { CalendarPlus } from "lucide-react";

import { ButtonLink } from "../../design-system/Button";
import { PageHeader } from "../../design-system/PageHeader";
import { Tabs } from "../../design-system/Tabs";
import { LeaveBalances } from "./LeaveBalances";
import { RequestTable } from "./RequestTable";

export function MyLeavePage() {
  return (
    <div>
      <PageHeader
        title="My leave"
        description="Your balances and requests. Days waiting for approval are held back from what you can use."
        action={
          <ButtonLink to="/leave/request">
            <CalendarPlus aria-hidden className="size-4" />
            Request leave
          </ButtonLink>
        }
      />
      <Tabs
        label="My leave"
        items={[
          {
            id: "requests",
            label: "Requests",
            content: (
              <RequestTable
                label="My leave requests"
                own
                showEmployee={false}
                empty={{
                  title: "No leave requests yet",
                  body: "Requests you make appear here with their status.",
                }}
              />
            ),
          },
          {
            id: "balances",
            label: "Balances and ledger",
            content: <LeaveBalances employeeId={null} ownLabel />,
          },
        ]}
      />
    </div>
  );
}
