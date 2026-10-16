/** Balance administration (`leave.balance.adjust`): one employee's balances and ledger with
 * adjustments; closing a leave year (carry-forward and lapse, previewed before anything is
 * recorded); and importing opening balances from a file, all or nothing. Never one's own
 * balance (SOD-6). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileUp } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ComboBox } from "../../design-system/ComboBox";
import { DataTable } from "../../design-system/DataTable";
import { TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Tabs } from "../../design-system/Tabs";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { humanize } from "../../lib/format";
import { options } from "../organization/queries";
import { useEmployeeSearch } from "../employees/queries";
import { LeaveBalances } from "./LeaveBalances";
import { LEAVE_KEY, formatDays, useLeaveTypes } from "./queries";

type YearEndResult = components["schemas"]["YearEndResult"];
type ImportResult = components["schemas"]["ImportResult"];
type ImportRow = components["schemas"]["OpeningBalanceRow"];

export function LeaveBalancesPage() {
  return (
    <div>
      <PageHeader
        title="Leave balances"
        description="Adjust an employee's balance, close a leave year, or import opening balances. You cannot change your own."
      />
      <Tabs
        label="Leave balances"
        items={[
          { id: "employee", label: "One employee", content: <OneEmployee /> },
          { id: "year-end", label: "Year end", content: <YearEnd /> },
          { id: "import", label: "Opening balances", content: <OpeningBalances /> },
        ]}
      />
    </div>
  );
}

function OneEmployee() {
  const search = useEmployeeSearch(null);
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  return (
    <div>
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

const OUTCOMES: Record<string, string> = {
  done: "Closed",
  would_do: "Would be closed",
  already_closed: "Already closed",
  no_policy: "No policy covers them",
  not_decided: "Year-end rule not decided in their policy",
  not_year_end: "Their leave year does not end on this date",
  skipped_own: "Your own balance, left for someone else",
  not_permitted: "Outside your scope",
};

function YearEnd() {
  const queryClient = useQueryClient();
  const types = useLeaveTypes();
  const [leaveType, setLeaveType] = useState<string | null>(null);
  const [lastDay, setLastDay] = useState("");
  const [result, setResult] = useState<YearEndResult | null>(null);
  const run = useMutation({
    mutationFn: (apply: boolean) =>
      unwrap(
        client.POST("/api/v1/leave-year-end", {
          body: { leave_type_id: leaveType ?? "", year_end: lastDay, apply },
        }),
      ),
    onSuccess: async (data) => {
      setResult(data);
      if (data.applied) await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
    },
  });
  const rows = (result?.items ?? []).filter(
    (row) => row.outcome !== "no_policy" && row.outcome !== "not_permitted",
  );
  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-prose text-sm text-neutral-600">
        For each person whose policy decides the year end, the balance on the last day is carried forward up
        to the policy's limit and the rest lapses or stays. Preview first: nothing is recorded until you close
        the year, and each person is closed once.
      </p>
      <Form
        className="grid max-w-2xl gap-4 sm:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!run.isPending) run.mutate(false);
        }}
      >
        <Select
          label="Leave type"
          name="leave_type_id"
          placeholder="Choose"
          options={options(types.data?.items)}
          value={leaveType}
          onChange={(value) => {
            setLeaveType(value);
            setResult(null);
          }}
        />
        <TextField
          label="Last day of the leave year"
          name="year_end"
          type="date"
          value={lastDay}
          onChange={(value) => {
            setLastDay(value);
            setResult(null);
          }}
        />
        <div className="sm:col-span-2">
          <Button
            type="submit"
            variant="secondary"
            isPending={run.isPending && !result}
            isDisabled={!leaveType || !lastDay}
          >
            Preview
          </Button>
        </div>
      </Form>
      {run.isError ? <ProblemMessage error={run.error} fallback="We couldn't run the year end." /> : null}
      {result ? (
        <>
          <Banner tone={result.applied ? "success" : "info"}>
            {result.applied ? "Year closed. " : "Preview only. "}
            {Object.entries(result.counts)
              .map(([key, count]) => `${OUTCOMES[key] ?? humanize(key)}: ${count}`)
              .join(" · ")}
          </Banner>
          {rows.length > 0 ? (
            <DataTable
              label="Year-end result"
              rows={rows}
              rowKey={(row) => row.employee.id}
              columns={[
                { key: "name", header: "Employee", primary: true, cell: (row) => row.employee.name },
                { key: "outcome", header: "Outcome", cell: (row) => OUTCOMES[row.outcome] ?? row.outcome },
                {
                  key: "closing",
                  header: "Closing",
                  align: "end",
                  cell: (row) => (row.closing ? formatDays(row.closing) : ""),
                },
                {
                  key: "carried",
                  header: "Carried",
                  align: "end",
                  cell: (row) => (row.carried ? formatDays(row.carried) : ""),
                },
                {
                  key: "lapsed",
                  header: "Lapses",
                  align: "end",
                  cell: (row) => (row.lapsed ? formatDays(row.lapsed) : ""),
                },
              ]}
            />
          ) : null}
          {!result.applied && (result.counts.would_do ?? 0) > 0 ? (
            <div>
              <Button
                onPress={() => {
                  run.mutate(true);
                }}
                isPending={run.isPending}
              >
                Close the leave year for {result.counts.would_do} people
              </Button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

const IMPORT_OUTCOMES: Record<string, string> = {
  ok: "Ready",
  recorded: "Recorded",
  unknown_employee: "No employee with this code",
  unknown_leave_type: "No leave type with this code",
  not_permitted: "Outside your scope",
  own_balance: "Your own balance",
  no_policy: "No policy covers them on that date",
  already_recorded: "Opening balance already recorded for that leave year",
  duplicate_row: "Repeated row",
  zero_days: "Zero days",
};

/** CSV with a header row: employee_code, leave_type_code, days, effective_date (YYYY-MM-DD). */
function parseCsv(text: string): ImportRow[] | string {
  const lines = text
    .replace(/^\uFEFF/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const header = (lines.shift() ?? "").split(",").map((cell) => cell.trim().toLowerCase());
  const index = (name: string) => header.indexOf(name);
  const columns = ["employee_code", "leave_type_code", "days", "effective_date"];
  if (columns.some((name) => index(name) < 0)) {
    return `The first row must name the columns: ${columns.join(", ")}.`;
  }
  const rows: ImportRow[] = [];
  for (const [number, line] of lines.entries()) {
    const cells = line.split(",").map((cell) => cell.trim().replace(/^"|"$/g, ""));
    const value = (name: string) => cells[index(name)] ?? "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value("effective_date")) || !/^-?\d+(\.\d{1,2})?$/.test(value("days"))) {
      return `Row ${number + 2}: the days must be a number with at most two decimals, and the date YYYY-MM-DD.`;
    }
    rows.push({
      employee_code: value("employee_code"),
      leave_type_code: value("leave_type_code"),
      days: value("days"),
      effective_date: value("effective_date"),
    });
  }
  if (rows.length === 0) return "The file has no rows after the header.";
  if (rows.length > 5000) return "Import at most 5000 rows at a time.";
  return rows;
}

function OpeningBalances() {
  const queryClient = useQueryClient();
  const [rows, setRows] = useState<ImportRow[] | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const send = useMutation({
    mutationFn: (apply: boolean) =>
      unwrap(
        client.POST("/api/v1/leave-opening-balances", {
          body: { reason: reason.trim(), rows: rows ?? [], apply },
        }),
      ),
    onSuccess: async (data) => {
      setResult(data);
      if (data.applied) await queryClient.invalidateQueries({ queryKey: LEAVE_KEY });
    },
  });
  const problems = (result?.items ?? []).filter((row) => row.outcome !== "ok" && row.outcome !== "recorded");
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <p className="text-sm text-neutral-600">
        A CSV file with a header row naming <code>employee_code</code>, <code>leave_type_code</code>,{" "}
        <code>days</code> and <code>effective_date</code>. Every row is checked first; a file with any refused
        row records nothing, so it can be fixed and sent again.
      </p>
      <label className="flex flex-col gap-1 text-sm font-semibold text-neutral-800">
        <span className="inline-flex items-center gap-2">
          <FileUp aria-hidden className="size-4" />
          CSV file
        </span>
        <input
          type="file"
          accept=".csv,text/csv"
          className="text-sm font-normal"
          onChange={(event) => {
            const file = event.target.files?.[0];
            setResult(null);
            if (!file) return;
            void file.text().then((text) => {
              const parsed = parseCsv(text);
              if (typeof parsed === "string") {
                setFileError(parsed);
                setRows(null);
              } else {
                setFileError(null);
                setRows(parsed);
              }
            });
          }}
        />
      </label>
      {fileError ? <Banner tone="danger">{fileError}</Banner> : null}
      {rows ? <p className="text-sm text-neutral-700">{rows.length} rows read from the file.</p> : null}
      <TextArea
        label="Reason"
        name="reason"
        value={reason}
        onChange={setReason}
        maxLength={500}
        description="Recorded on every entry, for example where the balances came from."
      />
      <div className="flex flex-wrap gap-2">
        <Button
          variant="secondary"
          onPress={() => {
            send.mutate(false);
          }}
          isPending={send.isPending}
          isDisabled={!rows || reason.trim() === ""}
        >
          Check the file
        </Button>
        {result && !result.applied && problems.length === 0 ? (
          <Button
            onPress={() => {
              send.mutate(true);
            }}
            isPending={send.isPending}
          >
            Record {rows?.length ?? 0} opening balances
          </Button>
        ) : null}
      </div>
      {send.isError ? <ProblemMessage error={send.error} fallback="We couldn't check the file." /> : null}
      {result ? (
        <Banner tone={result.applied ? "success" : problems.length > 0 ? "warning" : "info"}>
          {result.applied
            ? `Recorded ${result.counts.recorded ?? 0} opening balances.`
            : problems.length > 0
              ? `${problems.length} rows need fixing; nothing was recorded.`
              : "Every row is ready to record."}
        </Banner>
      ) : null}
      {problems.length > 0 ? (
        <DataTable
          label="Rows to fix"
          rows={problems}
          rowKey={(row) => String(row.line)}
          columns={[
            { key: "line", header: "Row", primary: true, cell: (row) => row.line + 1 },
            { key: "employee", header: "Employee code", cell: (row) => row.employee_code },
            { key: "type", header: "Leave type code", cell: (row) => row.leave_type_code },
            { key: "outcome", header: "Problem", cell: (row) => IMPORT_OUTCOMES[row.outcome] ?? row.outcome },
          ]}
        />
      ) : null}
    </div>
  );
}
