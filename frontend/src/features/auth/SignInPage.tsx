import { zodResolver } from "@hookform/resolvers/zod";
import { Form } from "react-aria-components";
import { Controller, useForm } from "react-hook-form";
import { Link, useNavigate, useSearchParams } from "react-router";
import { z } from "zod";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { TextField } from "../../design-system/TextField";
import { client, unwrap } from "../../lib/api";
import { signInProblemText } from "../../lib/forms";
import { ApiError } from "../../lib/problem";
import { backendReadiness, useBackendReadiness } from "../../lib/readiness";
import { AuthLayout } from "./AuthLayout";
import { ConnectionStatus } from "./ConnectionStatus";

const schema = z.object({
  email: z
    .string()
    .trim()
    .min(1, "Enter your work email.")
    .pipe(z.email("Enter an email address like name@example.com.")),
  password: z.string().min(1, "Enter your password."),
});
type Values = z.infer<typeof schema>;

/** No answer from the API (status 0), or a gateway in front of it that could not reach it. */
function apiUnreachable(error: unknown): boolean {
  return error instanceof ApiError && [0, 502, 503, 504].includes(error.problem.status);
}

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
  const phase = useBackendReadiness();

  // The sign-in request goes out only after the readiness check has passed (waiting for one
  // that is running, or starting a new one), and is never repeated automatically: it counts
  // towards lockout and issues a single-use challenge. If the check gives up, the status above
  // the form offers Retry and nothing is sent.
  const submit = form.handleSubmit(async (values) => {
    if (!(await backendReadiness.check())) return;
    try {
      const { mfa_token } = await unwrap(client.POST("/api/v1/auth/login", { body: values }));
      const state: VerifyState = { mfaToken: mfa_token, next: params.get("next") };
      void navigate("/sign-in/verify", { state });
    } catch (error) {
      if (apiUnreachable(error)) {
        backendReadiness.markStale();
        void backendReadiness.check();
      }
      form.setError("root", {
        message:
          error instanceof ApiError && error.problem.status === 0
            ? "We couldn't reach the HRMS, so you are not signed in. When the status above shows it is connected, select Continue again."
            : signInProblemText(error),
      });
    }
  });

  return (
    <AuthLayout title="Sign in">
      <div className="flex flex-col gap-4">
        <ConnectionStatus phase={phase} />
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
          <Link className="text-accent-700 underline" to="/password-reset">
            Forgot your password?
          </Link>{" "}
          Lost your authenticator and your recovery codes? Contact your HR team.
        </p>
      </div>
    </AuthLayout>
  );
}
