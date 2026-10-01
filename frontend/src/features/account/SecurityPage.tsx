/**
 * The user's own account security (AUTH-6, AUTH-7, docs/api-architecture.md §5): sessions,
 * authenticator apps, recovery codes, password and sign-in history. Changes that need
 * step-up go through `withStepUp`; the API enforces it either way.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { useStepUp } from "../../app/StepUp";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Section } from "../../design-system/Section";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { applyFieldErrors } from "../../lib/forms";
import { formatDateTime } from "../../lib/format";
import { AuthenticatorSetup, type TotpSetup } from "../auth/AuthenticatorSetup";
import { RecoveryCodes } from "../auth/RecoveryCodes";

const KEYS = {
  sessions: ["identity", "sessions"],
  factors: ["identity", "factors"],
  history: ["identity", "login-history"],
} as const;

export function SecurityPage() {
  return (
    <div>
      <PageHeader
        title="Account security"
        description="Where you are signed in, how you confirm it's you, and your password."
      />
      <SessionsSection />
      <AuthenticatorsSection />
      <RecoveryCodesSection />
      <PasswordSection />
      <HistorySection />
    </div>
  );
}

function SessionsSection() {
  const queryClient = useQueryClient();
  const sessions = useQuery({
    queryKey: KEYS.sessions,
    queryFn: () => unwrap(client.GET("/api/v1/me/sessions")),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: KEYS.sessions });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      unwrap(client.DELETE("/api/v1/me/sessions/{session_id}", { params: { path: { session_id: id } } })),
    onSettled: refresh,
  });
  const revokeOthers = useMutation({
    mutationFn: () => unwrap(client.DELETE("/api/v1/me/sessions")),
    onSettled: refresh,
  });

  return (
    <Section title="Where you're signed in" description="Sign out of any device you don't recognise.">
      {sessions.isPending ? <Skeleton label="Loading sessions" /> : null}
      {sessions.isError ? (
        <ProblemMessage error={sessions.error} fallback="We couldn't load your sessions." />
      ) : null}
      {revoke.isError || revokeOthers.isError ? (
        <ProblemMessage
          error={revoke.error ?? revokeOthers.error}
          fallback="We couldn't sign that session out."
        />
      ) : null}
      {sessions.data ? (
        <>
          <ul className="divide-y divide-neutral-200">
            {sessions.data.items.map((session) => (
              <li key={session.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                <div>
                  <p className="font-semibold">
                    {session.user_agent ?? "Unknown browser"}
                    {session.current ? <span className="ml-2 text-success-700">(this device)</span> : null}
                  </p>
                  <p className="text-neutral-600">
                    {session.ip ?? "Unknown address"} · last active {formatDateTime(session.last_activity_at)}
                  </p>
                </div>
                {!session.current ? (
                  <Button
                    variant="secondary"
                    onPress={() => {
                      revoke.mutate(session.id);
                    }}
                  >
                    Sign out this session
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
          {sessions.data.items.length > 1 ? (
            <div className="mt-3">
              <Button
                variant="secondary"
                isPending={revokeOthers.isPending}
                onPress={() => {
                  revokeOthers.mutate();
                }}
              >
                Sign out everywhere else
              </Button>
            </div>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}

function AuthenticatorsSection() {
  const queryClient = useQueryClient();
  const withStepUp = useStepUp();
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const factors = useQuery({
    queryKey: KEYS.factors,
    queryFn: () => unwrap(client.GET("/api/v1/me/mfa/factors")),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: KEYS.factors });

  const start = async () => {
    setFailure(null);
    try {
      setSetup(
        await withStepUp(() =>
          unwrap(client.POST("/api/v1/me/mfa/totp/setup", { body: { label: "Authenticator app" } })),
        ),
      );
    } catch (error) {
      setFailure(error);
    }
  };
  const confirm = async (code: string) => {
    if (setup === null) return;
    await withStepUp(() =>
      unwrap(client.POST("/api/v1/me/mfa/totp/confirm", { body: { factor_id: setup.factor_id, code } })),
    );
    setSetup(null);
    await refresh();
  };
  const remove = async (id: string) => {
    setFailure(null);
    try {
      await withStepUp(() =>
        unwrap(client.DELETE("/api/v1/me/mfa/factors/{factor_id}", { params: { path: { factor_id: id } } })),
      );
      await refresh();
    } catch (error) {
      setFailure(error);
    }
  };

  const items = factors.data?.items ?? [];
  return (
    <Section
      title="Authenticator apps"
      description="You confirm each sign-in with a code from one of these. You always keep at least one."
    >
      {factors.isPending ? <Skeleton label="Loading authenticator apps" /> : null}
      {factors.isError ? (
        <ProblemMessage error={factors.error} fallback="We couldn't load your authenticator apps." />
      ) : null}
      {failure ? (
        <ProblemMessage error={failure} fallback="We couldn't change your authenticator apps." />
      ) : null}
      <ul className="divide-y divide-neutral-200">
        {items.map((factor) => (
          <li key={factor.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
            <div>
              <p className="font-semibold">{factor.label}</p>
              <p className="text-neutral-600">
                Added {formatDateTime(factor.created_at)}
                {factor.last_used_at ? ` · last used ${formatDateTime(factor.last_used_at)}` : ""}
              </p>
            </div>
            {items.length > 1 ? (
              <Button variant="secondary" onPress={() => void remove(factor.id)}>
                Remove
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
      {setup ? (
        <div className="mt-4">
          <AuthenticatorSetup setup={setup} onConfirm={confirm} />
        </div>
      ) : (
        <div className="mt-3">
          <Button variant="secondary" onPress={() => void start()}>
            Add an authenticator app
          </Button>
        </div>
      )}
    </Section>
  );
}

function RecoveryCodesSection() {
  const withStepUp = useStepUp();
  const [codes, setCodes] = useState<string[] | null>(null);
  const regenerate = useMutation({
    mutationFn: () => withStepUp(() => unwrap(client.POST("/api/v1/me/mfa/recovery-codes"))),
    onSuccess: (result) => {
      setCodes(result.codes);
    },
  });
  return (
    <Section
      title="Recovery codes"
      description="Use one to sign in if you lose your phone. Making new codes stops the old ones working."
    >
      {regenerate.isError ? (
        <ProblemMessage error={regenerate.error} fallback="We couldn't make new codes." />
      ) : null}
      {codes ? (
        <RecoveryCodes
          codes={codes}
          continueLabel="Done"
          onContinue={() => {
            setCodes(null);
          }}
        />
      ) : (
        <Button
          variant="secondary"
          isPending={regenerate.isPending}
          onPress={() => {
            regenerate.mutate();
          }}
        >
          Make new recovery codes
        </Button>
      )}
    </Section>
  );
}

const passwordSchema = z
  .object({
    current_password: z.string().min(1, "Enter your current password."),
    new_password: z.string().min(12, "Use at least 12 characters.").max(128, "Use at most 128 characters."),
    confirmation: z.string(),
  })
  .refine((values) => values.new_password === values.confirmation, {
    path: ["confirmation"],
    message: "The two new passwords are different. Enter the same password twice.",
  });
type PasswordValues = z.infer<typeof passwordSchema>;

function PasswordSection() {
  const withStepUp = useStepUp();
  const [done, setDone] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const form = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { current_password: "", new_password: "", confirmation: "" },
  });
  const submit = form.handleSubmit(async ({ current_password, new_password }) => {
    setDone(false);
    setFailure(null);
    try {
      await withStepUp(() =>
        unwrap(client.POST("/api/v1/me/password", { body: { current_password, new_password } })),
      );
      form.reset();
      setDone(true);
    } catch (error) {
      if (!applyFieldErrors(error, form.setError, ["current_password", "new_password"])) setFailure(error);
    }
  });

  return (
    <Section title="Password" description="Changing your password signs you out on every other device.">
      {done ? <Banner tone="success">Your password is changed. Other devices are signed out.</Banner> : null}
      {failure ? <ProblemMessage error={failure} fallback="We couldn't change your password." /> : null}
      <Form className="flex max-w-sm flex-col gap-4" onSubmit={(event) => void submit(event)}>
        {(["current_password", "new_password", "confirmation"] as const).map((name) => (
          <Controller
            key={name}
            control={form.control}
            name={name}
            render={({ field, fieldState }) => (
              <TextField
                label={
                  {
                    current_password: "Current password",
                    new_password: "New password",
                    confirmation: "Enter the new password again",
                  }[name]
                }
                type="password"
                autoComplete={name === "current_password" ? "current-password" : "new-password"}
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
        ))}
        <Button type="submit" isPending={form.formState.isSubmitting}>
          Change password
        </Button>
      </Form>
    </Section>
  );
}

const EVENT_LABELS: Record<string, string> = {
  "login.succeeded": "Signed in",
  "login.failed": "Sign-in failed",
  "account.locked": "Account locked for 15 minutes",
};

function HistorySection() {
  const history = useQuery({
    queryKey: KEYS.history,
    queryFn: () => unwrap(client.GET("/api/v1/me/login-history", { params: { query: { limit: 20 } } })),
  });
  return (
    <Section title="Sign-in history" description="Sign-in activity on your account in the last 90 days.">
      {history.isPending ? <Skeleton label="Loading sign-in history" /> : null}
      {history.isError ? (
        <ProblemMessage error={history.error} fallback="We couldn't load your sign-in history." />
      ) : null}
      {history.data?.items.length === 0 ? (
        <p className="text-sm text-neutral-600">No sign-in activity yet.</p>
      ) : null}
      <ul className="divide-y divide-neutral-200 text-sm">
        {history.data?.items.map((item) => (
          <li key={`${item.occurred_at}-${item.event_type}`} className="py-2">
            <p className="font-semibold">{EVENT_LABELS[item.event_type] ?? item.event_type}</p>
            <p className="text-neutral-600">
              {formatDateTime(item.occurred_at)} · {item.ip ?? "Unknown address"}
            </p>
          </li>
        ))}
      </ul>
    </Section>
  );
}
