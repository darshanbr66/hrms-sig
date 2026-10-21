import { useState } from "react";
import { Form } from "react-aria-components";

import { Button } from "../../design-system/Button";
import { TextField } from "../../design-system/TextField";
import type { components } from "../../lib/api-schema";
import { ApiError } from "../../lib/problem";

export type TotpSetup = components["schemas"]["TotpSetup"];

const CODE_DIGITS = 6;

/** The key in groups of four, easier to read and type; authenticator apps ignore the spaces. */
export function groupedKey(secret: string): string {
  return secret.match(/.{1,4}/g)?.join(" ") ?? secret;
}

/** Digits only, at most six: apps show codes as "123 456", and pasted codes keep the space. */
export function codeDigits(value: string): string {
  return value.replace(/\D/g, "").slice(0, CODE_DIGITS);
}

/**
 * Step "Set up your authenticator app": scan the QR code, or copy the key into an app on the
 * same phone, then confirm with a code. The key is shown only here; the API never returns it
 * again.
 */
export function AuthenticatorSetup({
  setup,
  onConfirm,
}: {
  setup: TotpSetup;
  onConfirm: (code: string) => Promise<void>;
}) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [pending, setPending] = useState(false);
  const [copied, setCopied] = useState(false);

  const confirm = async () => {
    setPending(true);
    setError(undefined);
    try {
      await onConfirm(code);
    } catch (failure) {
      if (failure instanceof ApiError && failure.is("validation-error")) {
        setError(failure.problem.errors?.[0]?.message ?? "That code is not correct. Enter the current code.");
      } else {
        setError(failure instanceof ApiError ? failure.message : "We couldn't confirm the code. Try again.");
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-neutral-700">
        Open an authenticator app on your phone (any app that supports time-based codes works, for example
        Google Authenticator, Microsoft Authenticator or 1Password) and scan this code.
      </p>
      {/* White in both themes: a QR code needs dark modules on a light background to scan. The
          image carries its own white background and quiet zone. */}
      <img
        src={setup.qr_code}
        alt="QR code to add your Sigvitas HRMS account to an authenticator app"
        className="size-56 self-start rounded-[var(--radius-surface)] border border-neutral-200 bg-white"
      />
      <div className="text-sm text-neutral-700">
        <p>On the same phone? Add the account in your app with this key instead:</p>
        <p className="mt-1 flex flex-wrap items-center gap-2">
          <code className="rounded bg-neutral-100 px-2 py-1 font-mono text-sm break-all">
            {groupedKey(setup.secret)}
          </code>
          <Button
            variant="quiet"
            onPress={() => {
              void navigator.clipboard.writeText(setup.secret).then(() => {
                setCopied(true);
              });
            }}
          >
            {copied ? "Copied" : "Copy key"}
          </Button>
        </p>
        <p className="mt-2">
          This key is new each time this step starts. If you added Sigvitas HRMS to your app before, delete
          that entry: its codes will not work.
        </p>
      </div>
      <Form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          void confirm();
        }}
      >
        <TextField
          label="Enter the 6-digit code to confirm"
          name="totp-code"
          value={code}
          onChange={(value) => {
            setCode(codeDigits(value));
          }}
          inputMode="numeric"
          autoComplete="one-time-code"
          error={error}
        />
        <Button type="submit" isPending={pending}>
          Confirm authenticator
        </Button>
      </Form>
    </div>
  );
}
