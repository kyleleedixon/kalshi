import { sqlRead } from "@/lib/db";
import { fmtRelative } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function HeartbeatPage() {
  const staleSec = Number(process.env.NEXT_PUBLIC_HEARTBEAT_STALE_SEC ?? 45);
  const rows = await sqlRead`
    SELECT engine_id, last_beat, phase, version, notes FROM heartbeat
  `;
  return (
    <div>
      <h1 className="text-lg mb-3">heartbeat</h1>
      <p className="text-xs text-neutral-500 mb-3">
        Threshold: {staleSec}s. Stale heartbeat = engine dead
        (dead-man&apos;s-switch). Positions remain safe by construction — they
        live on Kalshi and settle on their own.
      </p>
      <table>
        <thead>
          <tr>
            <th>engine</th>
            <th>phase</th>
            <th>version</th>
            <th>last beat</th>
            <th>state</th>
            <th>notes</th>
          </tr>
        </thead>
        <tbody>
          {(rows as any[]).map((r) => {
            const ago = (Date.now() - new Date(r.last_beat).getTime()) / 1000;
            const fresh = ago <= staleSec;
            return (
              <tr key={r.engine_id}>
                <td className="font-mono">{r.engine_id}</td>
                <td>{r.phase}</td>
                <td>{r.version}</td>
                <td>{fmtRelative(r.last_beat)}</td>
                <td className={fresh ? "text-green-400" : "text-red-400"}>
                  {fresh ? `ok (${ago.toFixed(0)}s)` : `STALE (${ago.toFixed(0)}s)`}
                </td>
                <td className="text-neutral-500">{r.notes ?? ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
