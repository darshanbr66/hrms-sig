/**
 * Ask for a password reset link (AUTH-5). The answer is the same whether or not the email
 * belongs to an account, so this page never says which.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link } from "react-router";
import { z } from "zod";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { signInProblemText } from "../../lib/forms";
import { AuthLayout } from "./AuthLayout";

const schema = z.object({
  email: z
    .string()
    .trim()
    .min(1, "Enter your work email.")
    .pipe(z.email("Enter an email address like name@example.com.")),
});
type Values = z.infer<typeof schema>;

export function PasswordResetRequestPage() {
  const [sent, setSent] = useState(false);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "" } });

  const submit = form.handleSubmit(async (values) => {
    try {
      await unwrap(client.POST("/api/v1/auth/password-reset", { body: values }));
      setSent(true);
    } catch (error) {
      form.setError("root", { message: signInProblemText(error) });
    }
  });

  if (sent) {
    return (
      <AuthLayout title="Check your email">
        <div className="flex flex-col gap-4">
          <Banner tone="success">
            If an account uses that email, we have sent it a link to reset the password. The link works once
            and for a limited time.
          </Banner>
          <p className="text-sm text-neutral-700">
            Resetting your password does not change your authenticator app: you will still need it to sign in.
          </p>
          <Link className="text-sm text-accent-700 underline" to="/sign-in">
            Back to sign in
          </Link>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Reset your password">
      <div className="flex flex-col gap-4">
        {form.formState.errors.root?.message ? (
          <Banner tone="danger">{form.formState.errors.root.message}</Banner>
        ) : null}
        <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
          <Controller
            control={form.control}
            name="email"
            render={({ field, fieldState }) => (
              <TextField
                label="Work email"
                type="email"
                autoComplete="username"
                description="We'll email a link to reset your password."
                name={field.name}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={fieldState.error?.message}
              />
            )}
          />
          <Button type="submit" isPending={form.formState.isSubmitting}>
            Send reset link
          </Button>
        </Form>
        <Link className="text-sm text-accent-700 underline" to="/sign-in">
          Back to sign in
        </Link>
      </div>
    </AuthLayout>
  );
}
