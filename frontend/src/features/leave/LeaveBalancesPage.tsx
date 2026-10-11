/** Balance administration (`leave.balance.adjust`): find an employee, see their balances and
 * ledger, and record an adjustment or opening balance with a reason. */
import { useState } from "react";

import { ComboBox } from "../../design-system/ComboBox";
import { PageHeader } from "../../design-system/PageHeader";
import { useEmployeeSearch } from "../employees/queries";
import { LeaveBalances } from "./LeaveBalances";

export function LeaveBalancesPage() {
  const search = useEmployeeSearch(null);
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  return (
    <div>
      <PageHeader
        title="Leave balances"
        description="Find an employee to see their balances and ledger, and record adjustments or opening balances. You cannot adjust your own."
      />
      <div className="mb-6 max-w-md">
        <ComboBox
          label="Employee"
          options={search.options}
          selectedKey={employeeId}
          onSelectionChange={setEmployeeId}
          inputValue={search.text}
          onInputChange={search.setText}
          description="Search by name or employee code."
          emptyText="No employee you can see matches."
        />
      </div>
      {employeeId ? (
        <LeaveBalances employeeId={employeeId} />
      ) : (
        <p className="text-sm text-neutral-600">Choose an employee to see their leave.</p>
      )}
    </div>
  );
}
