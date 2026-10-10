/**
 * The employee directory (product-requirements EMP-6): every employee can find colleagues by
 * name and see the directory fields only (name, preferred name, designation, department,
 * location, work email). Nothing else is returned by the API.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { ContactRound, Mail } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { canSee } from "../../app/navigation";
import { hasPermission, useMe } from "../../app/session";
import { Avatar } from "../../design-system/Avatar";
import { EmptyState } from "../../design-system/EmptyState";
import { SearchField } from "../../design-system/Fields";
import { LoadMore } from "../../design-system/LoadMore";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Select } from "../../design-system/Select";
import { Skeleton } from "../../design-system/Skeleton";
import { client, unwrap } from "../../lib/api";
import { options, useDepartments, useLocations } from "../organization/queries";
import { PEOPLE_KEY } from "./queries";

const ANY = "any";

export function DirectoryPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const department = params.get("department") ?? ANY;
  const location = params.get("location") ?? ANY;
  const [text, setText] = useState(q);
  const canReadOrg = hasPermission(me.data, "org.read");
  const departments = useDepartments(false, canReadOrg);
  const locations = useLocations(false, canReadOrg);
  const canOpenRecords = canSee(me.data, {
    anyOf: ["employee.profile.read.team", "employee.profile.read.all"],
  });

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value === "" || value === ANY) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const list = useInfiniteQuery({
    queryKey: [...PEOPLE_KEY, "directory", q, department, location],
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/api/v1/directory", {
          params: {
            query: {
              ...(q ? { q } : {}),
              ...(department !== ANY ? { department_id: department } : {}),
              ...(location !== ANY ? { location_id: location } : {}),
              limit: 48,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const people = list.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered = Boolean(q) || department !== ANY || location !== ANY;

  return (
    <div>
      <PageHeader title="Directory" description="Find colleagues and how to reach them at work." />
      <div className="mb-5 grid gap-3 md:grid-cols-[minmax(0,2fr)_repeat(2,minmax(0,1fr))] md:items-end">
        <SearchField
          label="Search the directory"
          placeholder="Search by name or email"
          value={text}
          onChange={(value) => {
            setText(value);
            if (value === "") update("q", "");
          }}
          onSubmit={(value) => {
            update("q", value.trim());
          }}
        />
        {canReadOrg ? (
          <>
            <Select
              label="Department"
              name="department"
              options={[{ id: ANY, label: "All departments" }, ...options(departments.data?.items)]}
              value={department}
              onChange={(value) => {
                update("department", value);
              }}
            />
            <Select
              label="Location"
              name="location"
              options={[{ id: ANY, label: "All locations" }, ...options(locations.data?.items)]}
              value={location}
              onChange={(value) => {
                update("location", value);
              }}
            />
          </>
        ) : null}
      </div>
      {list.isPending ? <Skeleton label="Loading the directory" lines={6} /> : null}
      {list.isError ? (
        <ProblemMessage
          error={list.error}
          fallback="We couldn't load the directory."
          onRetry={() => void list.refetch()}
        />
      ) : null}
      {list.isSuccess && people.length === 0 ? (
        <EmptyState
          icon={ContactRound}
          title={filtered ? "Nobody matches this search" : "The directory is empty"}
        >
          {filtered
            ? "Check the spelling, or search by part of a name or email."
            : "Colleagues appear here once HR adds their employee records."}
        </EmptyState>
      ) : null}
      {people.length > 0 ? (
        <>
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {people.map((person) => (
              <li
                key={person.id}
                className="flex gap-3 rounded-[var(--radius-surface)] border border-neutral-200 bg-neutral-0 p-4"
              >
                <Avatar name={person.preferred_name ?? person.name} />
                <div className="min-w-0">
                  <p className="truncate font-semibold text-neutral-900">
                    {canOpenRecords ? (
                      <Link className="hover:underline" to={`/employees/${person.id}`}>
                        {person.preferred_name ?? person.name}
                      </Link>
                    ) : (
                      (person.preferred_name ?? person.name)
                    )}
                  </p>
                  {person.preferred_name ? (
                    <p className="truncate text-xs text-neutral-600">{person.name}</p>
                  ) : null}
                  <p className="mt-1 truncate text-sm text-neutral-700">
                    {person.designation?.name ?? "No designation"}
                  </p>
                  <p className="truncate text-sm text-neutral-600">
                    {[person.department?.name, person.location?.name].filter(Boolean).join(" · ") ||
                      "Not placed"}
                  </p>
                  {person.work_email ? (
                    <a
                      className="mt-2 inline-flex max-w-full items-center gap-1 truncate text-sm text-accent-700 hover:underline"
                      href={`mailto:${person.work_email}`}
                    >
                      <Mail aria-hidden className="size-3.5 shrink-0" />
                      <span className="truncate">{person.work_email}</span>
                    </a>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
          <LoadMore
            shown={people.length}
            hasMore={list.hasNextPage}
            isLoading={list.isFetchingNextPage}
            onLoad={() => void list.fetchNextPage()}
          />
        </>
      ) : null}
    </div>
  );
}
