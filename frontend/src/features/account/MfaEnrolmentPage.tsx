/**
 * Enrolment-only mode after signing in with a recovery code (docs/security-architecture.md §3.3):
 * the account can do nothing else until a new authenticator is confirmed. Confirming turns the
 * session into a full one.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";

import { meQueryKey } from "../../app/session";
import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";
import { PageHeader } from "../../design-system/PageHeader";
import { ProblemMessage } from "../../design-system/ProblemMessage";
import { client, unwrap } from "../../lib/api";
import { HOME } from "../../lib/navigation";
import { AuthenticatorSetup } from "../auth/AuthenticatorSetup";

export function MfaEnrolmentPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const start = useMutation({
    mutationFn: () =>
      unwrap(client.POST("/api/v1/me/mfa/totp/setup", { body: { label: "Authenticator app" } })),
  });

  const confirm = async (code: string) => {
    if (!start.data) return;
    await unwrap(
      client.POST("/api/v1/me/mfa/totp/confirm", { body: { factor_id: start.data.factor_id, code } }),
    );
    await queryClient.invalidateQueries({ queryKey: meQueryKey });
    void navigate(HOME, { replace: true });
  };

  return (
    <div>
      <PageHeader title="Set up a new authenticator app" />
      <div className="flex flex-col gap-4">
        <Banner tone="warning">
          You signed in with a recovery code. Set up an authenticator app on your new phone to finish signing
          in. Until then you can't use the rest of the HRMS.
        </Banner>
        {start.isError ? (
          <ProblemMessage error={start.error} fallback="We couldn't start the setup. Try again." />
        ) : null}
        {start.data ? (
          <AuthenticatorSetup setup={start.data} onConfirm={confirm} />
        ) : (
          <Button
            isPending={start.isPending}
            onPress={() => {
              start.mutate();
            }}
          >
            Start setup
          </Button>
        )}
      </div>
    </div>
  );
}
