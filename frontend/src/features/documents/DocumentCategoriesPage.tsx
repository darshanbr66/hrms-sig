/** Document categories (`document.category.manage`, DOC-2): each category's sensitivity decides
 * who may read its documents. Nothing is seeded: HR adds the categories Sigvitas uses. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderCog, Plus } from "lucide-react";
import { useState } from "react";
import { Form } from "react-aria-components";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { DataTable } from "../../design-system/DataTable";
import { Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox, TextArea } from "../../design-system/Fields";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { client, unwrap } from "../../lib/api";
import { fieldMessages, ifMatch } from "../../lib/problem";
import { DOCUMENTS_KEY, SENSITIVITY_LABELS, useCategories, type Category } from "./queries";

const SENSITIVITIES = [
  { id: "internal", label: "Internal: the employee if visible, their manager, HR" },
  { id: "personal", label: "Personal: the employee if visible, HR" },
  { id: "sensitive", label: "Sensitive: the employee if visible, HR admins after verifying" },
];
const YES_NO = [
  { id: "yes", label: "Yes" },
  { id: "no", label: "No" },
];

export function DocumentCategoriesPage() {
  const [showArchived, setShowArchived] = useState(false);
  const categories = useCategories(showArchived);
  const [editing, setEditing] = useState<Category | "new" | null>(null);
  const rows = categories.data?.items ?? [];
  return (
    <div>
      <PageHeader
        title="Document categories"
        description="A category's sensitivity decides who can read its documents. Whether employees see or upload documents in it is your choice."
        action={
          <Button
            onPress={() => {
              setEditing("new");
            }}
          >
            <Plus aria-hidden className="size-4" />
            Add category
          </Button>
        }
      />
      <div className="mb-4">
        <Checkbox isSelected={showArchived} onChange={setShowArchived}>
          Show archived categories
        </Checkbox>
      </div>
      {categories.isPending ? <Skeleton label="Loading categories" lines={4} /> : null}
      {categories.isError ? (
        <ProblemMessage
          error={categories.error}
          fallback="We couldn't load the categories."
          onRetry={() => void categories.refetch()}
        />
      ) : null}
      {categories.isSuccess && rows.length === 0 ? (
        <EmptyState icon={FolderCog} title="No document categories yet">
          Add the kinds of documents Sigvitas keeps, such as offer letters or ID proofs, with who may read
          them.
        </EmptyState>
      ) : null}
      {rows.length > 0 ? (
        <DataTable
          label="Document categories"
          rows={rows}
          rowKey={(row) => row.id}
          columns={[
            {
              key: "name",
              header: "Name",
              primary: true,
              cell: (row) => (
                <Button
                  variant="quiet"
                  size="sm"
                  onPress={() => {
                    setEditing(row);
                  }}
                >
                  {row.name}
                </Button>
              ),
            },
            {
              key: "code",
              header: "Code",
              cell: (row) => <code className="font-mono text-xs">{row.code}</code>,
            },
            { key: "sensitivity", header: "Sensitivity", cell: (row) => SENSITIVITY_LABELS[row.sensitivity] },
            {
              key: "employee",
              header: "Employees",
              cell: (row) =>
                row.employee_visible ? (row.employee_can_upload ? "See and upload" : "See") : "Do not see",
            },
            {
              key: "expiry",
              header: "Expiry date",
              cell: (row) => (row.requires_expiry ? "Required" : "Optional"),
            },
            {
              key: "status",
              header: "Status",
              cell: (row) => (
                <Badge tone={row.status === "active" ? "success" : "neutral"}>
                  {row.status === "active" ? "Active" : "Archived"}
                </Badge>
              ),
            },
          ]}
        />
      ) : null}
      {editing ? (
        <CategoryDialog
          category={editing === "new" ? null : editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

function yesNo(value: boolean | undefined): string | null {
  return value === undefined ? null : value ? "yes" : "no";
}

function CategoryDialog({ category, onClose }: { category: Category | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [code, setCode] = useState(category?.code ?? "");
  const [name, setName] = useState(category?.name ?? "");
  const [description, setDescription] = useState(category?.description ?? "");
  const [sensitivity, setSensitivity] = useState<string | null>(category?.sensitivity ?? null);
  const [visible, setVisible] = useState(yesNo(category?.employee_visible));
  const [upload, setUpload] = useState(yesNo(category?.employee_can_upload));
  const [expiry, setExpiry] = useState(yesNo(category?.requires_expiry));
  const [status, setStatus] = useState<string>(category?.status ?? "active");
  const archiving = category?.status === "active" && status === "archived";
  const [local, setLocal] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => {
      const values = {
        name,
        description: description.trim() === "" ? null : description.trim(),
        employee_visible: visible === "yes",
        employee_can_upload: visible === "yes" && upload === "yes",
        requires_expiry: expiry === "yes",
      };
      return category
        ? unwrap(
            client.PATCH("/api/v1/document-categories/{category_id}", {
              params: {
                path: { category_id: category.id },
                header: { "If-Match": ifMatch(category.version) },
              },
              body: { ...values, sensitivity: sensitivity as "internal", status: status as "active" },
            }),
          )
        : unwrap(
            client.POST("/api/v1/document-categories", {
              body: { ...values, code, sensitivity: sensitivity as "internal" },
            }),
          );
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: DOCUMENTS_KEY });
      toast(category ? "Category updated" : "Category added");
      onClose();
    },
  });
  const errors = { ...fieldMessages(save.error), ...local };
  return (
    <Dialog
      title={category ? `Edit ${category.name}` : "Add document category"}
      isOpen
      onOpenChange={onClose}
      size="lg"
    >
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          if (!category && code.trim() === "") problems.code = "Enter a short code.";
          if (name.trim() === "") problems.name = "Enter a name.";
          if (!sensitivity) problems.sensitivity = "Choose who may read these documents.";
          if (!visible) problems.employee_visible = "Choose yes or no.";
          if (visible === "yes" && !upload) problems.employee_can_upload = "Choose yes or no.";
          if (!expiry) problems.requires_expiry = "Choose yes or no.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        {save.isError && Object.keys(fieldMessages(save.error)).length === 0 ? (
          <ProblemMessage error={save.error} fallback="We couldn't save the category." />
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          {category ? null : (
            <TextField
              label="Code"
              name="code"
              value={code}
              onChange={setCode}
              maxLength={32}
              error={errors.code}
            />
          )}
          <TextField
            label="Name"
            name="name"
            value={name}
            onChange={setName}
            maxLength={200}
            error={errors.name}
          />
        </div>
        <TextArea
          label="Description (optional)"
          name="description"
          value={description}
          onChange={setDescription}
          maxLength={1000}
        />
        <Select
          label="Sensitivity"
          name="sensitivity"
          placeholder="Choose"
          options={SENSITIVITIES}
          value={sensitivity}
          onChange={setSensitivity}
          {...(category ? { description: "It cannot change once documents use the category." } : {})}
          error={errors.sensitivity}
        />
        <div className="grid gap-4 sm:grid-cols-3">
          <Select
            label="Employees see their documents"
            name="employee_visible"
            placeholder="Choose"
            options={YES_NO}
            value={visible}
            onChange={setVisible}
            error={errors.employee_visible}
          />
          {visible === "yes" ? (
            <Select
              label="Employees may upload"
              name="employee_can_upload"
              placeholder="Choose"
              options={YES_NO}
              value={upload}
              onChange={setUpload}
              error={errors.employee_can_upload}
            />
          ) : (
            <div />
          )}
          <Select
            label="Expiry date required"
            name="requires_expiry"
            placeholder="Choose"
            options={YES_NO}
            value={expiry}
            onChange={setExpiry}
            error={errors.requires_expiry}
          />
        </div>
        {category ? (
          <Select
            label="Status"
            name="status"
            options={[
              { id: "active", label: "Active" },
              { id: "archived", label: "Archived (no new documents)" },
            ]}
            value={status}
            onChange={setStatus}
          />
        ) : null}
        {archiving ? (
          <Banner tone="warning">
            Archiving stops new documents in this category. Documents already in it stay where they are, with
            their access rules, and nothing is deleted. You can make it active again later.
          </Banner>
        ) : null}
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant={archiving ? "danger" : "primary"} isPending={save.isPending}>
            {archiving ? "Archive category" : category ? "Save changes" : "Add category"}
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
