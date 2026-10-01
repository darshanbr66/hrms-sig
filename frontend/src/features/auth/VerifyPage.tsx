import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Navigate, useLocation, useNavigate } from "react-router";
import { z } from "zod";

import { meQueryKey } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { signInProblemText } from "../../lib/forms";
import { safeNext } from "../../lib/navigation";
import { ApiError } from "../../lib/problem";
import { AuthLayout } from "./AuthLayout";
import type { VerifyState } from "./SignInPage";

const schema = z.object({ code: z.string().trim().min(1, "Enter the code.") });
type Values = z.infer<typeof schema>;

function isVerifyState(value: unknown): value is VerifyState {
  return typeof value === "object" && value !== null && typeof (value as VerifyState).mfaToken === "string";
}

export function VerifyPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [useRecovery, setUseRecovery] = useState(false);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { code: "" } });

  const state: unknown = location.state;
  if (!isVerifyState(state)) {
    return <Navigate to="/sign-in" replace />;
  }
  const { mfaToken, next } = state;

  const submit = form.handleSubmit(async ({ code }) => {
    try {
      const body = useRecovery ? { mfa_token: mfaToken, recovery_code: code } : { mfa_token: mfaToken, code };
      const signedIn = await unwrap(client.POST("/api/v1/auth/login/mfa", { body }));
      await queryClient.invalidateQueries({ queryKey: meQueryKey });
      const destination =
        signedIn.session_scope === "mfa_enrolment" ? "/account/mfa-enrolment" : safeNext(next);
      void navigate(destination, { replace: true });
    } catch (error) {
      if (error instanceof ApiError && error.code === "login.challenge_expired") {
        void navigate("/sign-in?expired=1", { replace: true });
        return;
      }
      form.setError("root", { message: signInProblemText(error) });
    }
  });

  return (
    <AuthLayout title={useRecovery ? "Use a recovery code" : "Enter your code"}>
      <div className="flex flex-col gap-4">
        {form.formState.errors.root?.message ? (
          <Banner tone="danger">{form.formState.errors.root.message}</Banner>
        ) : null}
        <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
          <Controller
            control={form.control}
            name="code"
            render={({ field, fieldState }) => (
              <TextField
                label={useRecovery ? "Recovery code" : "6-digit code"}
                description={
                  useRecovery
                    ? "One of the codes you saved when you set up your account. Each code works once."
                    : "From the authenticator app on your phone."
                }
                inputMode={useRecovery ? "text" : "numeric"}
                autoComplete="one-time-code"
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Sign in
          </Button>
        </Form>
        <Button
          variant="quiet"
          onPress={() => {
            setUseRecovery(!useRecovery);
            form.reset({ code: "" });
          }}
        >
          {useRecovery ? "Use your authenticator app instead" : "Use a recovery code instead"}
        </Button>
        {useRecovery ? (
          <p className="text-sm text-neutral-600">
            After signing in with a recovery code you will set up a new authenticator app before you can
            continue.
          </p>
        ) : null}
      </div>
    </AuthLayout>
  );
}
