/**
 * Re-enrolment after an administrator reset the authenticator (docs/security-architecture.md §3.3).
 * The emailed link alone is not enough: the password is needed too. Success opens a session that
 * can only set up a new authenticator; the enrolment page takes over from there.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link, useNavigate, useParams } from "react-router";
import { z } from "zod";

import { ENROLMENT_PATH } from "../../app/guards";
import { meQueryKey } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { applyFieldErrors, signInProblemText } from "../../lib/forms";
import { ApiError } from "../../lib/problem";
import { AuthLayout } from "./AuthLayout";

const schema = z.object({ password: z.string().min(1, "Enter your password.") });
type Values = z.infer<typeof schema>;

export function MfaReenrolmentPage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [invalid, setInvalid] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { password: "" } });

  const submit = form.handleSubmit(async ({ password }) => {
    setFailure(null);
    try {
      await unwrap(
        client.POST("/api/v1/auth/mfa-reenrolment/{token}", {
          params: { path: { token } },
          body: { password },
        }),
      );
      await queryClient.invalidateQueries({ queryKey: meQueryKey });
      void navigate(`${ENROLMENT_PATH}?after=reset`, { replace: true });
    } catch (error) {
      if (error instanceof ApiError && error.code === "mfa_reenrolment.invalid") {
        setInvalid(true);
      } else if (!applyFieldErrors(error, form.setError, ["password"])) {
        setFailure(error);
      }
    }
  });

  if (invalid) {
    return (
      <AuthLayout title="This link doesn't work">
        <div className="flex flex-col gap-4">
          <Banner tone="danger">
            This link has expired or was already used. Ask your HR team to reset your authenticator again.
          </Banner>
          <Link className="text-sm text-accent-700 underline" to="/sign-in">
            Go to sign in
          </Link>
        </div>
      </AuthLayout>
    );
  }
  return (
    <AuthLayout title="Set up your authenticator app again">
      <div className="flex flex-col gap-4">
        <p className="text-sm text-neutral-700">
          An administrator reset the authenticator app on your account. Enter your password to continue. Next
          you will set up an authenticator app and get new recovery codes.
        </p>
        {failure ? <ProblemMessage error={null} fallback={signInProblemText(failure)} /> : null}
        <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
          <Controller
            control={form.control}
            name="password"
            render={({ field, fieldState }) => (
              <TextField
                label="Password"
                type="password"
                autoComplete="current-password"
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Continue
          </Button>
        </Form>
      </div>
    </AuthLayout>
  );
}
