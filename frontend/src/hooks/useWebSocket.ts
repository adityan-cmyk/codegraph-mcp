import { useEffect, useState } from "react";

export function useWebSocket(url: string) {
  const [status, setStatus] = useState<"idle" | "connecting" | "connected">("idle");

  useEffect(() => {
    if (!url) {
      return;
    }

    setStatus("connecting");
    setStatus("connected");
  }, [url]);

  return { status };
}