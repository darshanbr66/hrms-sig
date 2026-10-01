/**
 * Account activation from an invite link (docs/security-architecture.md §3.1,
 * ui-ux-guidelines.md "Account activation and MFA enrolment"):
 * 1. set a password, 2. set up an authenticator app, 3. save the recovery codes.
 * There is no "Skip": the account becomes active only when all three are done.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router";
import { z } from "zod";

import { meQueryKey } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { Skeleton } from "../../design-system/Skeleton";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { applyFieldErrors } from "../../lib/forms";
import { HOME } from "../../lib/navigation";
import { ApiError } from "../../lib/problem";
import { AuthenticatorSetup, type TotpSetup } from "./AuthenticatorSetup";
import { AuthLayout } from "./AuthLayout";
import { RecoveryCodes } from "./RecoveryCodes";

const passwordSchema = z
  .object({
    password: z.string().min(12, "Use at least 12 characters.").max(128, "Use at most 128 characters."),
    confirmation: z.string(),
  })
  .refine((values) => values.password === values.confirmation, {
    path: ["confirmation"],
    message: "The two passwords are different. Enter the same password twice.",
  });
type PasswordValues = z.infer<typeof passwordSchema>;

type Step =
  | { name: "password" }
  | { name: "authenticator"; enrolment: string; setup: TotpSetup }
  | { name: "codes"; codes: string[] };

const INVALID_INVITE = "This invite link has expired or was already used. Ask your HR team for a new invite.";

export function InvitePage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<Step>({ name: "password" });
  const [failure, setFailure] = useState<unknown>(null);
  const status = useQuery({
    queryKey: ["identity", "invite", token],
    queryFn: () => unwrap(client.GET("/api/v1/auth/invite/{token}", { params: { path: { token } } })),
    retry: false,
    staleTime: Infinity,
  });
  const form = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { password: "", confirmation: "" },
  });

  const handleFailure = (error: unknown) => {
    if (error instanceof ApiError && error.code === "invite.invalid") {
      setFailure(new ApiError({ ...error.problem, detail: INVALID_INVITE }));
      return;
    }
    setFailure(error);
  };

  const setPassword = form.handleSubmit(async ({ password }) => {
    setFailure(null);
    try {
      const path = { params: { path: { token } } };
      const { enrolment_token } = await unwrap(
        client.POST("/api/v1/auth/invite/{token}/password", { ...path, body: { password } }),
      );
      const setup = await unwrap(
        client.POST("/api/v1/auth/invite/{token}/mfa/totp/setup", { ...path, body: { enrolment_token } }),
      );
      setStep({ name: "authenticator", enrolment: enrolment_token, setup });
    } catch (error) {
      if (!applyFieldErrors(error, form.setError, ["password"])) {
        handleFailure(error);
      }
    }
  });

  const confirm = async (enrolment: string, setup: TotpSetup, code: string) => {
    const activated = await unwrap(
      client.POST("/api/v1/auth/invite/{token}/mfa/totp/confirm", {
        params: { path: { token } },
        body: { enrolment_token: enrolment, factor_id: setup.factor_id, code },
      }),
    );
    setStep({ name: "codes", codes: activated.recovery_codes });
  };

  if (status.isPending) {
    return (
      <AuthLayout title="Set up your account">
        <Skeleton label="Checking your invite" />
      </AuthLayout>
    );
  }
  if (status.isError || !status.data.valid) {
    return (
      <AuthLayout title="This invite link doesn't work">
        <Banner tone="danger">{INVALID_INVITE}</Banner>
      </AuthLayout>
    );
  }

  const greeting = status.data.first_name ? `Welcome, ${status.data.first_name}` : "Set up your account";
  return (
    <AuthLayout title={greeting}>
      <ol aria-label="Steps" className="mb-6 flex gap-2 text-xs text-neutral-600">
        {["Password", "Authenticator app", "Recovery codes"].map((label, index) => (
          <li key={label} aria-current={index === stepIndex(step) ? "step" : undefined}>
            <span className={index === stepIndex(step) ? "font-semibold text-accent-700" : undefined}>
              {index + 1}. {label}
            </span>
          </li>
        ))}
      </ol>
      {failure ? (
        <ProblemMessage error={failure} fallback="We couldn't complete this step. Try again." />
      ) : null}
      {step.name === "password" ? (
        <Form className="mt-4 flex flex-col gap-4" onSubmit={(event) => void setPassword(event)}>
          <h2 className="text-[1.125rem] font-semibold">Set your password</h2>
          <Controller
            control={form.control}
            name="password"
            render={({ field, fieldState }) => (
              <TextField
                label="Password"
                type="password"
                autoComplete="new-password"
                description="At least 12 characters. A few unrelated words make a strong password. Next, you will set up an authenticator app."
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
          <Controller
            control={form.control}
            name="confirmation"
            render={({ field, fieldState }) => (
              <TextField
                label="Enter the password again"
                type="password"
                autoComplete="new-password"
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Set password and continue
          </Button>
        </Form>
      ) : null}
      {step.name === "authenticator" ? (
        <section className="mt-4">
          <h2 className="mb-4 text-[1.125rem] font-semibold">Set up your authenticator app</h2>
          <AuthenticatorSetup
            setup={step.setup}
            onConfirm={(code) =>
              confirm(step.enrolment, step.setup, code).catch((error: unknown) => {
                if (error instanceof ApiError && error.code === "invite.invalid") handleFailure(error);
                throw error;
              })
            }
          />
        </section>
      ) : null}
      {step.name === "codes" ? (
        <section className="mt-4">
          <h2 className="mb-4 text-[1.125rem] font-semibold">Save your recovery codes</h2>
          <RecoveryCodes
            codes={step.codes}
            continueLabel="Go to your account"
            onContinue={() => {
              void queryClient
                .invalidateQueries({ queryKey: meQueryKey })
                .then(() => navigate(HOME, { replace: true }));
            }}
          />
        </section>
      ) : null}
    </AuthLayout>
  );
}

function stepIndex(step: Step): number {
  return { password: 0, authenticator: 1, codes: 2 }[step.name];
}
