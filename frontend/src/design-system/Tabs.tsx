import type { ReactNode } from "react";
import { Tab, TabList, TabPanel, Tabs as AriaTabs } from "react-aria-components";

export interface TabItem {
  id: string;
  label: string;
  content: ReactNode;
}

/** Sections of one record, switched in place with the keyboard or pointer. */
export function Tabs({
  label,
  items,
  selectedKey,
  onSelectionChange,
}: {
  label: string;
  items: readonly TabItem[];
  selectedKey?: string;
  onSelectionChange?: (key: string) => void;
}) {
  return (
    <AriaTabs
      {...(selectedKey ? { selectedKey } : {})}
      {...(onSelectionChange
        ? {
            onSelectionChange: (key) => {
              onSelectionChange(String(key));
            },
          }
        : {})}
    >
      <TabList aria-label={label} className="flex gap-1 overflow-x-auto border-b border-neutral-200">
        {items.map((item) => (
          <Tab
            key={item.id}
            id={item.id}
            className="-mb-px cursor-default whitespace-nowrap border-b-2 border-transparent px-3 py-2 text-sm font-semibold text-neutral-600 outline-none hover:text-neutral-900 focus-visible:outline-2 focus-visible:outline-accent-600 selected:border-accent-600 selected:text-accent-700"
          >
            {item.label}
          </Tab>
        ))}
      </TabList>
      {items.map((item) => (
        <TabPanel key={item.id} id={item.id} className="pt-5 outline-none">
          {item.content}
        </TabPanel>
      ))}
    </AriaTabs>
  );
}
