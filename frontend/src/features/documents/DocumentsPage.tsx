/** The signed-in person's documents and the company's documents (DOC-1 to DOC-6). */
import { hasPermission, useMe } from "../../app/session";
import { PageHeader } from "../../design-system/PageHeader";
import { Tabs, type TabItem } from "../../design-system/Tabs";
import { DocumentList } from "./DocumentList";

export function DocumentsPage() {
  const me = useMe();
  const tabs: TabItem[] = [];
  if (hasPermission(me.data, "document.read.self") && me.data?.employee_id) {
    tabs.push({
      id: "mine",
      label: "My documents",
      content: (
        <DocumentList
          owner={{ kind: "me" }}
          canUpload={hasPermission(me.data, "document.upload.self")}
          uploadableCategory={(category) => category.employee_can_upload}
          emptyText="Documents HR shares with you, and ones you upload, appear here."
        />
      ),
    });
  }
  if (hasPermission(me.data, "document.company.read") || hasPermission(me.data, "document.company.manage")) {
    tabs.push({
      id: "company",
      label: "Company documents",
      content: (
        <DocumentList
          owner={{ kind: "company" }}
          canUpload={hasPermission(me.data, "document.company.manage")}
          uploadableCategory={(category) => category.sensitivity === "internal"}
          emptyText="Policies and other company documents HR publishes appear here."
        />
      ),
    });
  }
  return (
    <div>
      <PageHeader
        title="Documents"
        description="Files are kept privately. Each download link works for one minute, and every download is recorded."
      />
      <Tabs label="Documents" items={tabs} />
    </div>
  );
}
