"use client";

import { useEffect, useState } from "react";

import { apiUrl } from "@/lib/api";

export type ApiHealthStatus = "checking" | "online" | "offline";

/**
 * Poll the backend health endpoint and report connectivity.
 * Runs client-side only; never executed during the production build.
 */
export function useApiHealth(intervalMs = 30_000): ApiHealthStatus {
  const [status, setStatus] = useState<ApiHealthStatus>("checking");

  useEffect(() => {
    let cancelled = false;

    const check = async () => {
      try {
        const response = await fetch(apiUrl("/api/health"), {
          cache: "no-store",
        });
        if (!cancelled) {
          setStatus(response.ok ? "online" : "offline");
        }
      } catch {
        if (!cancelled) {
          setStatus("offline");
        }
      }
    };

    void check();
    const timer = setInterval(() => void check(), intervalMs);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [intervalMs]);

  return status;
}
