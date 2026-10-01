/**
 * System and security settings (docs/database-design.md §4.11). Everyone with `settings.read`
 * sees the values; only holders of a setting's own permission see the controls to change it,
 * and every change needs step-up. The API decides either way.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";

import { useStepUp } from "../../app/StepUp";
import { hasPermission, useMe } from "../../app/session";
import { Button } from "../../design-system/Button";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Section } from "../../design-system/Section";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import type { components } from "../../lib/api-schema";
import { ApiError } from "../../lib/problem";

type Setting = components["schemas"]["SettingView"];

const KEY = ["settings", "list"] as const;

export function SettingsPage() {
  const settings = useQuery({ queryKey: KEY, queryFn: () => unwrap(client.GET("/api/v1/settings")) });
  const items = settings.data?.items ?? [];
  const security = items.filter((item) => item.key.startsWith("security."));
  const other = items.filter((item) => !item.key.startsWith("security."));
  return (
    <div>
      <PageHeader
        title="Settings"
        description="Security and delivery settings. Changes are recorded in the audit log and need you to confirm it's you."
      />
      {settings.isPending ? <Skeleton label="Loading settings" /> : null}
      {settings.isError ? (
        <ProblemMessage error={settings.error} fallback="We couldn't load the settings." />
      ) : null}
      {security.length > 0 ? (
        <Section title="Sign-in and sessions">
          <SettingList items={security} />
        </Section>
      ) : null}
      {other.length > 0 ? (
        <Section title="Email delivery">
          <SettingList items={other} />
        </Section>
      ) : null}
    </div>
  );
}

function SettingList({ items }: { items: Setting[] }) {
  return (
    <ul className="divide-y divide-neutral-200">
      {items.map((item) => (
        <li key={item.key} className="py-4">
          <SettingRow setting={item} />
        </li>
      ))}
    </ul>
  );
}

function SettingRow({ setting }: { setting: Setting }) {
  const me = useMe();
  const queryClient = useQueryClient();
  const withStepUp = useStepUp();
  const [value, setValue] = useState(String(setting.value));
  const [error, setError] = useState<string | undefined>();
  const canChange = hasPermission(me.data, setting.permission);
  const save = useMutation({
    mutationFn: (next: number | null) =>
      withStepUp(() =>
        unwrap(
          client.PUT("/api/v1/settings/{key}", {
            params: { path: { key: setting.key } },
            body: { value: next },
          }),
        ),
      ),
    onSuccess: async (saved) => {
      setValue(String(saved.value));
      setError(undefined);
      await queryClient.invalidateQueries({ queryKey: KEY });
    },
    onError: (failure) => {
      if (failure instanceof ApiError && failure.is("validation-error")) {
        setError(failure.problem.errors?.[0]?.message ?? "Check the value.");
      }
    },
  });

  const submit = () => {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < setting.minimum || parsed > setting.maximum) {
      setError(`Enter a whole number from ${setting.minimum} to ${setting.maximum}.`);
      return;
    }
    save.mutate(parsed);
  };

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-semibold text-neutral-900">{setting.description}</p>
      <p className="text-sm text-neutral-600">
        Now {setting.value} {setting.unit}
        {setting.overridden ? ` (default ${setting.default})` : " (the default)"}
      </p>
      {save.isError && !(save.error instanceof ApiError && save.error.is("validation-error")) ? (
        <ProblemMessage error={save.error} fallback="We couldn't save this setting." />
      ) : null}
      {canChange ? (
        <Form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <TextField
            label={`New value (${setting.unit}, ${setting.minimum} to ${setting.maximum})`}
            name={setting.key}
            value={value}
            onChange={setValue}
            inputMode="numeric"
            error={error}
          />
          <Button type="submit" variant="secondary" isPending={save.isPending}>
            Save
          </Button>
          {setting.overridden ? (
            <Button
              variant="quiet"
              onPress={() => {
                save.mutate(null);
              }}
            >
              Restore default
            </Button>
          ) : null}
        </Form>
      ) : null}
    </div>
  );
}
