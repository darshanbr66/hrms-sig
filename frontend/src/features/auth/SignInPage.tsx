import { zodResolver } from "@hookform/resolvers/zod";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { useNavigate, useSearchParams } from "react-router";
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
  password: z.string().min(1, "Enter your password."),
});
type Values = z.infer<typeof schema>;

/** Carried in navigation state, in memory only: the single-use token is never stored. */
export interface VerifyState {
  mfaToken: string;
  next: string | null;
}

export function SignInPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: "", password: "" } });
  const failure = form.formState.errors.root?.message;

  const submit = form.handleSubmit(async (values) => {
    try {
      const { mfa_token } = await unwrap(client.POST("/api/v1/auth/login", { body: values }));
      const state: VerifyState = { mfaToken: mfa_token, next: params.get("next") };
      void navigate("/sign-in/verify", { state });
    } catch (error) {
      form.setError("root", { message: signInProblemText(error) });
    }
  });

  return (
    <AuthLayout title="Sign in">
      <div className="flex flex-col gap-4">
        {params.get("expired") ? <Banner>Your session ended. Sign in again to continue.</Banner> : null}
        {failure ? <Banner tone="danger">{failure}</Banner> : null}
        <Form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
          <Controller
            control={form.control}
            name="email"
            render={({ field, fieldState }) => (
              <TextField
                label="Work email"
                type="email"
                autoComplete="username"
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
        <p className="text-sm text-neutral-600">
          Forgot your password or lost your authenticator? Contact your HR team.
        </p>
      </div>
    </AuthLayout>
  );
}
