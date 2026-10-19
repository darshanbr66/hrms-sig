/**
 * A list of documents with their versions, for one employee, the signed-in person, or the
 * company. Uploading and new versions follow what the API says the viewer may do; sensitive
 * documents stay hidden until the viewer verifies with their authenticator.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileText, Upload } from "lucide-react";
import { useId, useState } from "react";
import { Form } from "react-aria-components";

import { Badge } from "../../design-system/Badge";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ConfirmDialog, Dialog, DialogActions } from "../../design-system/Dialog";
import { EmptyState } from "../../design-system/EmptyState";
import { Checkbox } from "../../design-system/Fields";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { useToast } from "../../design-system/Toast";
import { meQueryKey } from "../../app/session";
import { client, unwrap } from "../../lib/api";
import { formatDate } from "../../lib/format";
import { fieldMessages, ifMatch, problemText } from "../../lib/problem";
import {
  DOCUMENTS_KEY,
  MAX_FILE_BYTES,
  SCAN_LABELS,
  SCAN_TONES,
  SENSITIVITY_LABELS,
  formatBytes,
  uploadFile,
  useCategories,
  useDownload,
  useRefreshDocuments,
  type Category,
  type DocumentView,
} from "./queries";

type Owner = { kind: "me" } | { kind: "employee"; id: string } | { kind: "company" };

function listPath(owner: Owner): string {
  if (owner.kind === "me") return "/api/v1/me/documents";
  if (owner.kind === "company") return "/api/v1/company-documents";
  return `/api/v1/employees/${owner.id}/documents`;
}

export function DocumentList({
  owner,
  canUpload,
  uploadableCategory,
  emptyText,
}: {
  owner: Owner;
  /** Whether to offer uploading: the API still decides each upload. */
  canUpload: boolean;
  /** Which categories to offer for upload. */
  uploadableCategory: (category: Category) => boolean;
  emptyText: string;
}) {
  const [showArchived, setShowArchived] = useState(false);
  const [uploading, setUploading] = useState(false);
  const list = useQuery({
    queryKey: [...DOCUMENTS_KEY, "list", owner, showArchived],
    queryFn: () => {
      if (owner.kind === "me") return unwrap(client.GET("/api/v1/me/documents"));
      if (owner.kind === "company")
        return unwrap(
          client.GET("/api/v1/company-documents", { params: { query: { include_archived: showArchived } } }),
        );
      return unwrap(
        client.GET("/api/v1/employees/{employee_id}/documents", {
          params: { path: { employee_id: owner.id }, query: { include_archived: showArchived } },
        }),
      );
    },
  });
  if (list.isPending) return <Skeleton label="Loading documents" lines={4} />;
  if (list.isError) {
    return (
      <ProblemMessage
        error={list.error}
        fallback="We couldn't load the documents."
        onRetry={() => void list.refetch()}
      />
    );
  }
  const data = list.data;
  return (
    <div className="flex flex-col gap-4">
      {!data.storage_ready ? (
        <Banner tone="warning">
          File storage is not set up, so documents cannot be uploaded or downloaded yet. Ask your system
          administrator.
        </Banner>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        {owner.kind === "me" ? (
          <span />
        ) : (
          <Checkbox isSelected={showArchived} onChange={setShowArchived}>
            Show archived documents
          </Checkbox>
        )}
        {canUpload && data.storage_ready ? (
          <Button
            onPress={() => {
              setUploading(true);
            }}
          >
            <Upload aria-hidden className="size-4" />
            Upload a document
          </Button>
        ) : null}
      </div>
      {data.sensitive_hidden > 0 ? (
        <SensitiveNotice count={data.sensitive_hidden} onVerified={() => void list.refetch()} />
      ) : null}
      {data.items.length === 0 ? (
        <EmptyState icon={FileText} title="No documents">
          {emptyText}
        </EmptyState>
      ) : (
        <ul aria-label="Documents" className="flex flex-col gap-3">
          {data.items.map((document) => (
            <DocumentCard key={document.id} document={document} storageReady={data.storage_ready} />
          ))}
        </ul>
      )}
      {uploading ? (
        <UploadDialog
          owner={owner}
          uploadableCategory={uploadableCategory}
          onClose={() => {
            setUploading(false);
          }}
        />
      ) : null}
    </div>
  );
}

/** Sensitive documents need a recent step-up to be listed: verifying shows them. */
function SensitiveNotice({ count, onVerified }: { count: number; onVerified: () => void }) {
  return (
    <Banner tone="info">
      <p>
        {count} sensitive {count === 1 ? "document is" : "documents are"} hidden until you confirm it&apos;s
        you with your authenticator app.
      </p>
      <div className="mt-2">
        <StepUpButton onVerified={onVerified} />
      </div>
    </Banner>
  );
}

/** Verifies with the authenticator (step-up) and reports success. */
function StepUpButton({ onVerified }: { onVerified: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const verify = useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/auth/step-up", { body: { code: code.trim() } })),
    onSuccess: async () => {
      setOpen(false);
      setCode("");
      await queryClient.invalidateQueries({ queryKey: meQueryKey });
      onVerified();
    },
  });
  return (
    <>
      <Button
        size="sm"
        variant="secondary"
        onPress={() => {
          setOpen(true);
        }}
      >
        Show sensitive documents
      </Button>
      <Dialog title="Confirm it's you" isOpen={open} onOpenChange={setOpen}>
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!verify.isPending) verify.mutate();
          }}
        >
          <TextField
            label="Code from your authenticator app"
            name="code"
            value={code}
            onChange={setCode}
            inputMode="numeric"
            autoComplete="one-time-code"
            error={
              verify.isError ? problemText(verify.error, "That code didn't work. Try a new one.") : undefined
            }
          />
          <DialogActions>
            <Button
              variant="secondary"
              onPress={() => {
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" isPending={verify.isPending}>
              Verify
            </Button>
          </DialogActions>
        </Form>
      </Dialog>
    </>
  );
}

function DocumentCard({ document, storageReady }: { document: DocumentView; storageReady: boolean }) {
  const download = useDownload();
  const refresh = useRefreshDocuments();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const archive = useMutation({
    mutationFn: () =>
      unwrap(
        client.PATCH("/api/v1/documents/{document_id}", {
          params: { path: { document_id: document.id }, header: { "If-Match": ifMatch(document.version) } },
          body: { status: document.status === "active" ? "archived" : "active" },
        }),
      ),
    onSuccess: async () => {
      await refresh();
      toast(document.status === "active" ? "Document archived" : "Document restored");
      setArchiving(false);
    },
  });
  return (
    <li className="rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold text-neutral-900">{document.title}</p>
          <p className="text-sm text-neutral-600">
            {document.category.name} · {SENSITIVITY_LABELS[document.category.sensitivity]}
            {document.expires_on ? ` · expires ${formatDate(document.expires_on, { weekday: false })}` : ""}
            {document.owner_name ? ` · ${document.owner_name}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {document.status === "archived" ? <Badge tone="neutral">Archived</Badge> : null}
          {document.can_add_version && document.status === "active" && storageReady ? (
            <Button
              size="sm"
              variant="secondary"
              onPress={() => {
                setAdding(true);
              }}
            >
              New version<span className="sr-only"> of {document.title}</span>
            </Button>
          ) : null}
          {document.can_manage ? (
            <Button
              size="sm"
              variant="quiet"
              onPress={() => {
                setArchiving(true);
              }}
            >
              {document.status === "active" ? "Archive" : "Restore"}
              <span className="sr-only"> {document.title}</span>
            </Button>
          ) : null}
        </div>
      </div>
      <ul aria-label={`Versions of ${document.title}`} className="mt-3 flex flex-col gap-2">
        {document.versions.map((version) => (
          <li key={version.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="min-w-0 text-neutral-800">
              Version {version.version_no}: {version.original_filename}{" "}
              <span className="text-neutral-500">
                ({formatBytes(version.size_bytes)},{" "}
                {formatDate(version.uploaded_at.slice(0, 10), { weekday: false })})
              </span>
            </span>
            <span className="flex items-center gap-2">
              <Badge tone={SCAN_TONES[version.scan_status]}>{SCAN_LABELS[version.scan_status]}</Badge>
              {version.scan_status === "clean" && storageReady ? (
                <Button
                  size="sm"
                  variant="quiet"
                  onPress={() => {
                    download.mutate({ documentId: document.id, versionId: version.id });
                  }}
                  isPending={download.isPending && download.variables.versionId === version.id}
                >
                  <Download aria-hidden className="size-4" />
                  Download<span className="sr-only"> version {version.version_no}</span>
                </Button>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
      {download.isError ? (
        <div className="mt-2">
          <ProblemMessage error={download.error} fallback="We couldn't prepare the download." />
        </div>
      ) : null}
      {adding ? (
        <VersionDialog
          document={document}
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
      <ConfirmDialog
        title={document.status === "active" ? `Archive ${document.title}?` : `Restore ${document.title}?`}
        isOpen={archiving}
        onOpenChange={setArchiving}
        confirmLabel={document.status === "active" ? "Archive document" : "Restore document"}
        onConfirm={() => {
          archive.mutate();
        }}
        isPending={archive.isPending}
        tone={document.status === "active" ? "danger" : "primary"}
      >
        {document.status === "active"
          ? "It no longer appears in lists unless archived documents are shown. Its versions are kept."
          : "It appears in lists again."}
        {archive.isError ? (
          <ProblemMessage error={archive.error} fallback="We couldn't change the document." />
        ) : null}
      </ConfirmDialog>
    </li>
  );
}

/** The browser's own file picker, kept native for its keyboard and screen reader support; the
 * error is linked to it like the other fields' errors. */
function FileInput({ onFile, error }: { onFile: (file: File | null) => void; error?: string | undefined }) {
  const errorId = useId();
  return (
    <div className="flex flex-col gap-1">
      <label className="flex flex-col gap-1 text-sm font-semibold text-neutral-800">
        File (PDF, PNG or JPEG, at most 10 MB)
        <input
          type="file"
          accept="application/pdf,image/png,image/jpeg"
          className="text-sm font-normal"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) => {
            onFile(event.target.files?.[0] ?? null);
          }}
        />
      </label>
      {error ? (
        <p id={errorId} className="text-sm text-danger-700">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function UploadDialog({
  owner,
  uploadableCategory,
  onClose,
}: {
  owner: Owner;
  uploadableCategory: (category: Category) => boolean;
  onClose: () => void;
}) {
  const categories = useCategories();
  const refresh = useRefreshDocuments();
  const toast = useToast();
  const choices = (categories.data?.items ?? []).filter(uploadableCategory);
  const [category, setCategory] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [expires, setExpires] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [local, setLocal] = useState<Record<string, string>>({});
  const chosen = choices.find((item) => item.id === category);
  const save = useMutation({
    mutationFn: () =>
      uploadFile(
        listPath(owner),
        { category_id: category ?? "", title: title.trim(), ...(expires ? { expires_on: expires } : {}) },
        file as File,
      ),
    onSuccess: async () => {
      await refresh();
      toast("Document uploaded. It can be downloaded once the virus check finishes.");
      onClose();
    },
  });
  const errors = { ...fieldMessages(save.error), ...local };
  return (
    <Dialog title="Upload a document" isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          const problems: Record<string, string> = {};
          if (!category) problems.category_id = "Choose a category.";
          if (title.trim() === "") problems.title = "Enter a title.";
          if (chosen?.requires_expiry && !expires)
            problems.expires_on = "This category needs an expiry date.";
          if (!file) problems.file = "Choose a file.";
          else if (file.size > MAX_FILE_BYTES) problems.file = "Choose a file of at most 10 MB.";
          setLocal(problems);
          if (Object.keys(problems).length === 0 && !save.isPending) save.mutate();
        }}
      >
        {save.isError && !errors.file && !errors.category_id ? (
          <ProblemMessage error={save.error} fallback="We couldn't upload the document." />
        ) : null}
        {categories.isSuccess && choices.length === 0 ? (
          <Banner tone="info">There is no category you can upload to. Ask HR.</Banner>
        ) : null}
        <Select
          label="Category"
          name="category_id"
          placeholder="Choose"
          options={choices.map((item) => ({ id: item.id, label: item.name }))}
          value={category}
          onChange={setCategory}
          error={errors.category_id}
        />
        <TextField
          label="Title"
          name="title"
          value={title}
          onChange={setTitle}
          maxLength={200}
          error={errors.title}
        />
        <TextField
          label={chosen?.requires_expiry ? "Expires on" : "Expires on (optional)"}
          name="expires_on"
          type="date"
          value={expires}
          onChange={setExpires}
          error={errors.expires_on}
        />
        <FileInput onFile={setFile} error={errors.file} />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Upload
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}

function VersionDialog({ document, onClose }: { document: DocumentView; onClose: () => void }) {
  const refresh = useRefreshDocuments();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [local, setLocal] = useState<string | undefined>();
  const save = useMutation({
    mutationFn: () => uploadFile(`/api/v1/documents/${document.id}/versions`, {}, file as File),
    onSuccess: async () => {
      await refresh();
      toast("New version uploaded");
      onClose();
    },
  });
  return (
    <Dialog title={`New version of ${document.title}`} isOpen onOpenChange={onClose}>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!file) setLocal("Choose a file.");
          else if (file.size > MAX_FILE_BYTES) setLocal("Choose a file of at most 10 MB.");
          else {
            setLocal(undefined);
            if (!save.isPending) save.mutate();
          }
        }}
      >
        <p className="text-sm text-neutral-700">Earlier versions stay available.</p>
        {save.isError ? (
          <ProblemMessage error={save.error} fallback="We couldn't upload the new version." />
        ) : null}
        <FileInput onFile={setFile} error={local ?? fieldMessages(save.error).file} />
        <DialogActions>
          <Button variant="secondary" onPress={onClose}>
            Cancel
          </Button>
          <Button type="submit" isPending={save.isPending}>
            Upload version
          </Button>
        </DialogActions>
      </Form>
    </Dialog>
  );
}
