import type { ReactNode } from "react";
import "@copilotkit/react-ui/styles.css";

export const metadata = {
  title: "Sentinel Co-pilot",
  description: "AG-UI/CopilotKit front over the Sentinel OpenAI-compat shim",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
