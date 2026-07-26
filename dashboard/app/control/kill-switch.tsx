"use client";
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

export default function KillSwitchToggle({ current }: { current: boolean }) {
  const [pending, startTransition] = useTransition();
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  async function onClick() {
    setErr(null);
    startTransition(async () => {
      const res = await fetch("/api/control/kill-switch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ active: !current }),
      });
      if (!res.ok) {
        setErr(`failed: ${res.status} ${await res.text()}`);
        return;
      }
      router.refresh();
    });
  }

  return (
    <div className="flex flex-col items-end gap-2">
      <button
        disabled={pending}
        onClick={onClick}
        className={`px-3 py-2 rounded font-mono text-sm border ${
          current
            ? "border-green-700 text-green-400 hover:bg-green-950"
            : "border-red-700 text-red-400 hover:bg-red-950"
        } disabled:opacity-50`}
      >
        {pending ? "…" : current ? "disable" : "engage"}
      </button>
      {err && <div className="text-xs text-red-400">{err}</div>}
    </div>
  );
}
