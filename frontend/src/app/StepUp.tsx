/**
 * Step-up (docs/security-architecture.md §3.6, ui-ux-guidelines.md "Step-up").
 *
 * `withStepUp(action)` runs an action; if the API answers `step-up-required`, a dialog asks
 * for a code from the authenticator app, verifies it, and runs the action again. A password
 * is never accepted and there is no fallback. The API enforces step-up; this only asks for it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { Form } from "react-aria-components";

import { Button } from "../design-system/Button";
import { Dialog } from "../design-system/Dialog";
import { TextField } from "../design-system/TextField";
import { client, unwrap } from "../lib/api";
import { ApiError } from "../lib/problem";
import { meQueryKey } from "./session";

type WithStepUp = <T>(action: () => Promise<T>) => Promise<T>;

const StepUpContext = createContext<WithStepUp | null>(null);

export function useStepUp(): WithStepUp {
  const value = useContext(StepUpContext);
  if (value === null) {
    throw new Error("useStepUp must be used inside StepUpProvider");
  }
  return value;
}

interface Pending {
  resolve: () => void;
  reject: (error: unknown) => void;
  original: ApiError;
}

export function StepUpProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [submitting, setSubmitting] = useState(false);
  const pending = useRef<Pending | null>(null);

  const askForCode = useCallback(
    (original: ApiError) =>
      new Promise<void>((resolve, reject) => {
        pending.current = { resolve, reject, original };
        setCode("");
        setError(undefined);
        setOpen(true);
      }),
    [],
  );

  const withStepUp = useCallback<WithStepUp>(
    async (action) => {
      try {
        return await action();
      } catch (failure) {
        if (!(failure instanceof ApiError) || !failure.is("step-up-required")) {
          throw failure;
        }
        await askForCode(failure);
        return action();
      }
    },
    [askForCode],
  );

  const cancel = () => {
    setOpen(false);
    pending.current?.reject(pending.current.original);
    pending.current = null;
  };

  const submit = async () => {
    setSubmitting(true);
    try {
      await unwrap(client.POST("/api/v1/auth/step-up", { body: { code } }));
      await queryClient.invalidateQueries({ queryKey: meQueryKey });
      setOpen(false);
      pending.current?.resolve();
      pending.current = null;
    } catch (failure) {
      if (failure instanceof ApiError && failure.is("rate-limited")) {
        setError("Too many attempts. Wait a few minutes, then try again.");
      } else if (failure instanceof ApiError && failure.is("validation-error")) {
        setError(failure.problem.errors?.[0]?.message ?? "That code is not correct. Enter the current code.");
      } else {
        setError(failure instanceof ApiError ? failure.message : "We couldn't check the code. Try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <StepUpContext.Provider value={withStepUp}>
      {children}
      <Dialog
        title="Confirm it's you"
        isOpen={open}
        onOpenChange={(isOpen) => {
          if (!isOpen) cancel();
        }}
      >
        <Form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <p className="text-sm text-neutral-700">Enter the 6-digit code from your authenticator app.</p>
          <TextField
            label="Code"
            name="step-up-code"
            value={code}
            onChange={setCode}
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            error={error}
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onPress={cancel}>
              Cancel
            </Button>
            <Button type="submit" isPending={submitting}>
              Confirm
            </Button>
          </div>
        </Form>
      </Dialog>
    </StepUpContext.Provider>
  );
}
