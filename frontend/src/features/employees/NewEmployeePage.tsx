/**
 * Add an employee record with their first job (`employee.create`). The status follows from the
 * joining date: joining soon before it, active from it. Inviting the person to the HRMS is a
 * separate step on their record, so a record can exist before they need an account.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { z } from "zod";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { PageHeader } from "../../design-system/PageHeader";
import { Panel } from "../../design-system/Panel";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { applyFieldErrors } from "../../lib/forms";
import { problemText } from "../../lib/problem";
import { useDepartments, useDesignations, useLocations } from "../organization/queries";
import { JobFields } from "./JobFields";
import { PEOPLE_KEY, type EmploymentType } from "./queries";

const optional = (max: number) =>
  z
    .string()
    .trim()
    .max(max, `Use at most ${max} characters.`)
    .transform((value) => (value === "" ? null : value));

const schema = z.object({
  employee_code: z
    .string()
    .trim()
    .min(1, "Enter the employee code.")
    .max(32, "Use at most 32 characters.")
    .regex(/^\S+$/, "Use no spaces in the code."),
  legal_first_name: z.string().trim().min(1, "Enter the legal first name.").max(100),
  legal_last_name: optional(100),
  preferred_name: optional(100),
  work_email: z
    .string()
    .trim()
    .refine(
      (value) => value === "" || /^[^@\s]+@[^@\s]+$/.test(value),
      "Enter an email like name@example.com.",
    )
    .transform((value) => (value === "" ? null : value)),
  work_phone: optional(32),
  date_of_joining: z.string().min(1, "Choose the joining date."),
  job: z.object({
    department_id: z.string().min(1, "Choose a department."),
    designation_id: z.string().min(1, "Choose a designation."),
    location_id: z.string().min(1, "Choose a location."),
    manager_employee_id: z.string().nullable(),
    employment_type: z.string().min(1, "Choose the employment type."),
  }),
});
type Input = z.input<typeof schema>;
type Values = z.output<typeof schema>;

const FIELDS = [
  "employee_code",
  "legal_first_name",
  "legal_last_name",
  "preferred_name",
  "work_email",
  "work_phone",
  "date_of_joining",
  "job.department_id",
  "job.designation_id",
  "job.location_id",
  "job.manager_employee_id",
  "job.employment_type",
] as const;

export function NewEmployeePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const departments = useDepartments();
  const designations = useDesignations();
  const locations = useLocations();
  const form = useForm<Input, unknown, Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      employee_code: "",
      legal_first_name: "",
      legal_last_name: "",
      preferred_name: "",
      work_email: "",
      work_phone: "",
      date_of_joining: "",
      job: {
        department_id: "",
        designation_id: "",
        location_id: "",
        manager_employee_id: null,
        employment_type: "",
      },
    },
  });

  const submit = form.handleSubmit(async (values) => {
    try {
      const created = await unwrap(
        client.POST("/api/v1/employees", {
          body: {
            ...values,
            job: { ...values.job, employment_type: values.job.employment_type as EmploymentType },
          },
        }),
      );
      await queryClient.invalidateQueries({ queryKey: PEOPLE_KEY });
      toast(`${created.name} added`);
      void navigate(`/employees/${created.id}`);
    } catch (error) {
      if (!applyFieldErrors(error, form.setError, FIELDS)) {
        form.setError("root", { message: problemText(error, "We couldn't add the employee. Try again.") });
      }
    }
  });

  const missingSetup = [
    departments.isSuccess && departments.data.items.length === 0 ? "a department" : null,
    designations.isSuccess && designations.data.items.length === 0 ? "a designation" : null,
    locations.isSuccess && locations.data.items.length === 0 ? "a location" : null,
  ].filter(Boolean);

  const text = (
    name: (typeof FIELDS)[number],
    label: string,
    extra: Partial<Parameters<typeof TextField>[0]> = {},
  ) => (
    <Controller
      control={form.control}
      name={name}
      render={({ field, fieldState }) => (
        <TextField
          label={label}
          name={field.name}
          value={(field.value as string | null | undefined) ?? ""}
          onChange={field.onChange}
          onBlur={field.onBlur}
          error={fieldState.error?.message}
          {...extra}
        />
      )}
    />
  );

  return (
    <div className="max-w-3xl">
      <PageHeader
        title="Add employee"
        description="Create the employee record and their first job. You can invite them to the HRMS from their record afterwards."
      />
      {missingSetup.length > 0 ? (
        <div className="mb-4">
          <Banner tone="warning">
            Add {missingSetup.join(", ")} in{" "}
            <Link className="underline" to="/organization">
              Organization
            </Link>{" "}
            before adding employees.
          </Banner>
        </div>
      ) : null}
      {form.formState.errors.root?.message ? (
        <div className="mb-4">
          <Banner tone="danger">{form.formState.errors.root.message}</Banner>
        </div>
      ) : null}
      <Form className="flex flex-col gap-6" onSubmit={(event) => void submit(event)}>
        <Panel title="Identity and contact">
          <div className="grid gap-4 sm:grid-cols-2">
            {text("employee_code", "Employee code", { maxLength: 32 })}
            {text("date_of_joining", "Date of joining", { type: "date" })}
            {text("legal_first_name", "Legal first name", { maxLength: 100 })}
            {text("legal_last_name", "Legal last name (optional)", { maxLength: 100 })}
            {text("preferred_name", "Preferred name (optional)", { maxLength: 100 })}
            {text("work_email", "Work email (optional)", { type: "email", maxLength: 254 })}
            {text("work_phone", "Work phone (optional)", { type: "tel", maxLength: 32 })}
          </div>
        </Panel>
        <Panel title="First job" description="Effective from the joining date.">
          <JobFields control={form.control} prefix="job." initialManager={null} />
        </Panel>
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onPress={() => {
              void navigate("/employees");
            }}
          >
            Cancel
          </Button>
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Add employee
          </Button>
        </div>
      </Form>
    </div>
  );
}
