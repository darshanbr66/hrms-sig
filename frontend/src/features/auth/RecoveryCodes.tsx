import { Check } from "lucide-react";
import { useState } from "react";
import { CheckboxButton, CheckboxField } from "react-aria-components";

import { Banner } from "../../design-system/Banner";
import { Button } from "../../design-system/Button";

/**
 * Step "Save your recovery codes": shown once, with copy and download, and a required
 * confirmation before continuing (ui-ux-guidelines.md, Account activation).
 */
export function RecoveryCodes({
  codes,
  continueLabel,
  onContinue,
}: {
  codes: readonly string[];
  continueLabel: string;
  onContinue: () => void;
}) {
  const [saved, setSaved] = useState(false);
  const text = codes.join("\n");

  const download = () => {
    const url = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "sigvitas-hrms-recovery-codes.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex flex-col gap-4">
      <Banner tone="warning">
        These codes are shown only once. Each one lets you sign in if you lose your phone, and works one time.
      </Banner>
      <ul aria-label="Recovery codes" className="grid grid-cols-2 gap-2 font-mono text-sm">
        {codes.map((code) => (
          <li key={code} className="rounded bg-neutral-100 px-2 py-1">
            {code}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onPress={() => void navigator.clipboard.writeText(text)}>
          Copy codes
        </Button>
        <Button variant="secondary" onPress={download}>
          Download codes
        </Button>
      </div>
      <CheckboxField isSelected={saved} onChange={setSaved}>
        <CheckboxButton className="flex items-center gap-2 text-sm">
          {({ isSelected }) => (
            <>
              <span
                aria-hidden
                className={`flex size-4 items-center justify-center rounded border ${
                  isSelected ? "border-accent-600 bg-accent-600 text-neutral-0" : "border-neutral-400"
                }`}
              >
                {isSelected ? <Check className="size-3" /> : null}
              </span>
              I have saved these codes
            </>
          )}
        </CheckboxButton>
      </CheckboxField>
      <Button isDisabled={!saved} onPress={onContinue}>
        {continueLabel}
      </Button>
    </div>
  );
}
