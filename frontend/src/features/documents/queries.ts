/** Documents (product-requirements DOC-1 to DOC-6). Files go to the API as the request body;
 * downloads are 60-second links the API issues after authorizing and auditing them. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { BadgeTone } from "../../design-system/Badge";
import { CSRF_HEADER, client, refreshSession, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { ApiError, networkProblem, toApiError } from "../../lib/problem";

export type DocumentView = components["schemas"]["DocumentView"];
export type Category = components["schemas"]["CategoryView"];
export type Version = DocumentView["versions"][number];

export const DOCUMENTS_KEY = ["documents"] as const;
export const MAX_FILE_BYTES = 10 * 1024 * 1024;

export const SENSITIVITY_LABELS: Record<Category["sensitivity"], string> = {
  internal: "Internal",
  personal: "Personal",
  sensitive: "Sensitive",
};

export const SCAN_LABELS: Record<Version["scan_status"], string> = {
  pending: "Being checked",
  clean: "Ready",
  infected: "Blocked: failed the virus check",
  failed: "Blocked: could not be checked",
};

export const SCAN_TONES: Record<Version["scan_status"], BadgeTone> = {
  pending: "info",
  clean: "success",
  infected: "danger",
  failed: "danger",
};

export function useCategories(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: [...DOCUMENTS_KEY, "categories", includeArchived],
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/document-categories", {
          params: { query: { include_archived: includeArchived } },
        }),
      ),
    enabled,
  });
}

/** Sends a file as the request body. The typed client sends JSON only, so this uses fetch
 * with the same CSRF header and one refresh on an expired session. */
export async function uploadFile(
  path: string,
  query: Record<string, string>,
  file: File,
): Promise<DocumentView> {
  const url = `${path}?${new URLSearchParams(query).toString()}`;
  const send = () =>
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        ...CSRF_HEADER,
        "Content-Type": "application/octet-stream",
        "X-File-Name": encodeURIComponent(file.name),
      },
      body: file,
    });
  let response: Response;
  try {
    response = await send();
    if (response.status === 401 && (await refreshSession())) response = await send();
  } catch {
    throw new ApiError(networkProblem());
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw toApiError(body, response);
  return body as DocumentView;
}

/** Asks for a download link and opens it; the browser saves the file. */
export function useDownload() {
  return useMutation({
    mutationFn: async ({ documentId, versionId }: { documentId: string; versionId: string }) => {
      const link = await unwrap(
        client.POST("/api/v1/documents/{document_id}/versions/{version_id}/download", {
          params: { path: { document_id: documentId, version_id: versionId } },
        }),
      );
      window.location.assign(link.url);
      return link;
    },
  });
}

export function useRefreshDocuments() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: DOCUMENTS_KEY });
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
