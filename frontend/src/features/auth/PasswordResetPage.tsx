/**
 * Choose a new password from a reset link. Success ends every session and does not sign in:
 * the next sign-in still needs the authenticator app.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link, useParams } from "react-router";
import { z } from "zod";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { applyFieldErrors } from "../../lib/forms";
import { ApiError } from "../../lib/problem";
import { AuthLayout } from "./AuthLayout";

const schema = z
  .object({
    password: z.string().min(12, "Use at least 12 characters.").max(128, "Use at most 128 characters."),
    confirmation: z.string(),
  })
  .refine((values) => values.password === values.confirmation, {
    path: ["confirmation"],
    message: "The two passwords are different. Enter the same password twice.",
  });
type Values = z.infer<typeof schema>;

type State = "form" | "done" | "invalid";

export function PasswordResetPage() {
  const { token = "" } = useParams();
  const [state, setState] = useState<State>("form");
  const [failure, setFailure] = useState<unknown>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { password: "", confirmation: "" },
  });

  const submit = form.handleSubmit(async ({ password }) => {
    setFailure(null);
    try {
      await unwrap(
        client.POST("/api/v1/auth/password-reset/{token}", {
          params: { path: { token } },
          body: { password },
        }),
      );
      setState("done");
    } catch (error) {
      if (error instanceof ApiError && error.code === "password_reset.invalid") {
        setState("invalid");
      } else if (!applyFieldErrors(error, form.setError, ["password"])) {
        setFailure(error);
      }
    }
  });

  if (state === "done") {
    return (
      <AuthLayout title="Your password is changed">
        <div className="flex flex-col gap-4">
          <Banner tone="success">
            You have been signed out everywhere. Sign in with your new password and your authenticator app.
          </Banner>
          <Link className="text-sm text-accent-700 underline" to="/sign-in">
            Go to sign in
          </Link>
        </div>
      </AuthLayout>
    );
  }
  if (state === "invalid") {
    return (
      <AuthLayout title="This link doesn't work">
        <div className="flex flex-col gap-4">
          <Banner tone="danger">
            This password reset link has expired or was already used. Ask for a new one.
          </Banner>
          <Link className="text-sm text-accent-700 underline" to="/password-reset">
            Ask for a new link
          </Link>
        </div>
      </AuthLayout>
    );
  }
  return (
    <AuthLayout title="Choose a new password">
      <div className="flex flex-col gap-4">
        {failure ? (
          <ProblemMessage error={failure} fallback="We couldn't change your password. Try again." />
        ) : null}
        <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
          {(["password", "confirmation"] as const).map((name) => (
            <Controller
              key={name}
              control={form.control}
              name={name}
              render={({ field, fieldState }) => (
                <TextField
                  label={name === "password" ? "New password" : "Enter the new password again"}
                  type="password"
                  autoComplete="new-password"
                  {...(name === "password"
                    ? { description: "At least 12 characters. A few unrelated words make a strong password." }
                    : {})}
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
      </div>
    </AuthLayout>
  );
}
