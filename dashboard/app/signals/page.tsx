import { sqlRead } from "@/lib/db";
import { fmtPct, fmtRelative } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function SignalsPage() {
  const rows = await sqlRead`
    SELECT s.id, s.adjusted_p, s.kalshi_bid, s.kalshi_ask, s.fee_bps,
           s.edge_net, s.calibration_confidence, s.rank, s.created_at,
           c.contract_id, c.underlying, c.domain
      FROM signals s
      LEFT JOIN contracts c ON c.id = s.contract_pk
     WHERE s.created_at > NOW() - INTERVAL '10 minutes'
     ORDER BY s.edge_net * s.calibration_confidence DESC
     LIMIT 200
  `;
  return (
    <div>
      <h1 className="text-lg mb-3">live edges (last 10 min)</h1>
      <table>
        <thead>
          <tr>
            <th>rank</th>
            <th>contract</th>
            <th>domain</th>
            <th>underlying</th>
            <th>adj p</th>
            <th>bid</th>
            <th>ask</th>
            <th>edge (net)</th>
            <th>calib conf</th>
            <th>when</th>
          </tr>
        </thead>
        <tbody>
          {(rows as any[]).map((r) => (
            <tr key={r.id}>
              <td>{r.rank ?? "—"}</td>
              <td className="font-mono">{r.contract_id ?? "—"}</td>
              <td>{r.domain}</td>
              <td>{r.underlying}</td>
              <td>{fmtPct(r.adjusted_p, 1)}</td>
              <td>{fmtPct(r.kalshi_bid, 1)}</td>
              <td>{fmtPct(r.kalshi_ask, 1)}</td>
              <td className={r.edge_net > 0 ? "text-green-400" : "text-neutral-500"}>
                {(r.edge_net * 100).toFixed(2)}c
              </td>
              <td>{fmtPct(r.calibration_confidence, 0)}</td>
              <td className="text-neutral-500">{fmtRelative(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
