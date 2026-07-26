import { sqlRead } from "@/lib/db";
import KillSwitchToggle from "./kill-switch";

export const dynamic = "force-dynamic";

export default async function ControlPage() {
  const rows = await sqlRead`SELECT key, value, updated_at, updated_by FROM control`;
  const map = Object.fromEntries((rows as any[]).map((r) => [r.key, r]));
  const kill = map["kill_switch"]?.value?.active ?? true;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg mb-3">control plane</h1>
        <p className="text-xs text-neutral-500">
          Writes go directly to the Neon <code>control</code> table. The engine
          re-reads every 3s and fails closed (kill-switch = ACTIVE) if the read
          fails.
        </p>
      </div>

      <section className="border border-neutral-800 rounded p-4 max-w-md">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm text-neutral-400">kill switch</div>
            <div
              className={`text-2xl font-mono ${
                kill ? "text-red-400" : "text-green-400"
              }`}
            >
              {kill ? "ACTIVE" : "off"}
            </div>
          </div>
          <KillSwitchToggle current={kill} />
        </div>
        <p className="mt-3 text-xs text-neutral-500">
          When ACTIVE, the engine rejects new opens and closes any existing
          positions on next cycle.
        </p>
      </section>

      <section>
        <h2 className="text-md text-neutral-300 mb-2">raw control rows</h2>
        <pre className="text-xs bg-neutral-900 p-3 rounded overflow-auto">
          {JSON.stringify(rows, null, 2)}
        </pre>
      </section>
    </div>
  );
}
