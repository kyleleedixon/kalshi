import { sqlRead } from "@/lib/db";
import { fmtRelative } from "@/lib/format";

export const dynamic = "force-dynamic";

type Row = Record<string, unknown>;

async function overview() {
  const [hb, control, calib, signalsCount, ordersCount] = await Promise.all([
    sqlRead`SELECT engine_id, last_beat, phase, version, notes FROM heartbeat`,
    sqlRead`SELECT key, value, updated_at FROM control`,
    sqlRead`SELECT generated_at, phase_gate_min_sample, jsonb_array_length(bands) AS n_bands
             FROM calibration_snapshots ORDER BY generated_at DESC LIMIT 1`,
    sqlRead`SELECT COUNT(*)::int AS n FROM signals WHERE created_at > NOW() - INTERVAL '1 hour'`,
    sqlRead`SELECT COUNT(*)::int AS n FROM paper_orders WHERE created_at > NOW() - INTERVAL '1 hour'`,
  ]);
  return { hb, control, calib, signalsCount, ordersCount };
}

export default async function Home() {
  const staleSec = Number(process.env.NEXT_PUBLIC_HEARTBEAT_STALE_SEC ?? 45);
  const d = await overview();
  const controlMap = Object.fromEntries(
    (d.control as Row[]).map((r) => [r.key as string, r])
  );
  const kill = (controlMap["kill_switch"]?.value as any)?.active ?? true;
  const gates = (controlMap["phase_gates"]?.value as any) ?? {};

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-lg mb-3">status</h1>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Card
            label="kill switch"
            value={kill ? "ACTIVE" : "off"}
            tone={kill ? "danger" : "ok"}
          />
          <Card
            label="signals (1h)"
            value={(d.signalsCount as Row[])[0]?.n as any}
          />
          <Card
            label="paper orders (1h)"
            value={(d.ordersCount as Row[])[0]?.n as any}
          />
          <Card
            label="latest calibration"
            value={
              (d.calib as Row[])[0]
                ? fmtRelative((d.calib as Row[])[0].generated_at as string)
                : "—"
            }
          />
        </div>
      </section>

      <section>
        <h2 className="text-lg mb-3">engines</h2>
        <table>
          <thead>
            <tr>
              <th>engine_id</th>
              <th>phase</th>
              <th>version</th>
              <th>last beat</th>
              <th>fresh?</th>
              <th>notes</th>
            </tr>
          </thead>
          <tbody>
            {(d.hb as Row[]).map((r) => {
              const ago =
                (Date.now() - new Date(r.last_beat as string).getTime()) / 1000;
              const fresh = ago <= staleSec;
              return (
                <tr key={r.engine_id as string}>
                  <td className="font-mono">{r.engine_id as string}</td>
                  <td>{r.phase as string}</td>
                  <td>{r.version as string}</td>
                  <td>{fmtRelative(r.last_beat as string)}</td>
                  <td
                    className={fresh ? "text-green-400" : "text-red-400"}
                    title={`${ago.toFixed(1)}s`}
                  >
                    {fresh ? "yes" : "STALE"}
                  </td>
                  <td className="text-neutral-500">{(r.notes as string) ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section>
        <h2 className="text-lg mb-3">phase gates</h2>
        <table>
          <thead>
            <tr>
              <th>domain</th>
              <th>operator unlocked</th>
              <th>min sample</th>
              <th>min brier improvement</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(gates).map(([domain, g]: [string, any]) => (
              <tr key={domain}>
                <td>{domain}</td>
                <td className={g.unlocked ? "text-green-400" : "text-neutral-400"}>
                  {g.unlocked ? "yes" : "no"}
                </td>
                <td>{g.min_settled_sample}</td>
                <td>{g.min_brier_improvement}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-neutral-500">
          Operator unlock is necessary but not sufficient. Calibration store
          gates ultimately determine per-domain unlock at decision time.
        </p>
      </section>
    </div>
  );
}

function Card({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "danger" | "ok";
}) {
  const toneClass =
    tone === "danger"
      ? "text-red-400"
      : tone === "ok"
        ? "text-green-400"
        : "text-neutral-100";
  return (
    <div className="border border-neutral-800 rounded p-3">
      <div className="text-xs text-neutral-500 uppercase tracking-wide">
        {label}
      </div>
      <div className={`text-xl font-mono mt-1 ${toneClass}`}>{value}</div>
    </div>
  );
}
