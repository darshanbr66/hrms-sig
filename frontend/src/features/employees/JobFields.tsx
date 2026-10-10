/** The placement fields shared by "Add employee" and "Record a job change". */
import { Controller, type Control, type FieldValues, type Path } from "react-hook-form";

import { ComboBox } from "../../design-system/ComboBox";
import { Select } from "../../design-system/Select";
import { options, useDepartments, useDesignations, useLocations } from "../organization/queries";
import { EMPLOYMENT_TYPES, useEmployeeSearch } from "./queries";

export interface JobValues {
  department_id: string;
  designation_id: string;
  location_id: string;
  manager_employee_id: string | null;
  employment_type: string;
}

export function JobFields<T extends FieldValues, U extends FieldValues = T>({
  control,
  prefix,
  initialManager,
  excludeEmployeeId,
}: {
  control: Control<T, unknown, U>;
  /** Where the job values sit in the form ("job." for the create form, "" for a job change). */
  prefix: "" | "job.";
  initialManager: { id: string; name: string } | null;
  excludeEmployeeId?: string;
}) {
  const departments = useDepartments();
  const designations = useDesignations();
  const locations = useLocations();
  const managers = useEmployeeSearch(initialManager, excludeEmployeeId);
  const name = (field: keyof JobValues) => `${prefix}${field}` as Path<T>;
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Controller
        control={control}
        name={name("department_id")}
        render={({ field, fieldState }) => (
          <Select
            label="Department"
            name={field.name}
            options={options(departments.data?.items)}
            value={field.value || null}
            onChange={field.onChange}
            placeholder="Choose a department"
            error={fieldState.error?.message}
          />
        )}
      />
      <Controller
        control={control}
        name={name("designation_id")}
        render={({ field, fieldState }) => (
          <Select
            label="Designation"
            name={field.name}
            options={options(designations.data?.items)}
            value={field.value || null}
            onChange={field.onChange}
            placeholder="Choose a designation"
            error={fieldState.error?.message}
          />
        )}
      />
      <Controller
        control={control}
        name={name("location_id")}
        render={({ field, fieldState }) => (
          <Select
            label="Location"
            name={field.name}
            options={options(locations.data?.items)}
            value={field.value || null}
            onChange={field.onChange}
            placeholder="Choose a location"
            error={fieldState.error?.message}
          />
        )}
      />
      <Controller
        control={control}
        name={name("employment_type")}
        render={({ field, fieldState }) => (
          <Select
            label="Employment type"
            name={field.name}
            options={EMPLOYMENT_TYPES}
            value={field.value || null}
            onChange={field.onChange}
            placeholder="Choose a type"
            error={fieldState.error?.message}
          />
        )}
      />
      <div className="sm:col-span-2">
        <Controller
          control={control}
          name={name("manager_employee_id")}
          render={({ field, fieldState }) => (
            <ComboBox
              label="Reports to (optional)"
              description="Search by name or employee code. Leave empty for someone with no manager."
              options={managers.options}
              selectedKey={field.value ?? null}
              onSelectionChange={(key) => {
                field.onChange(key);
              }}
              inputValue={managers.text}
              onInputChange={(value) => {
                managers.setText(value);
                if (value === "") field.onChange(null);
              }}
              error={fieldState.error?.message}
            />
          )}
        />
      </div>
    </div>
  );
}
