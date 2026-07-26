import { sqlRead } from "@/lib/db";
import { fmtRelative } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function LedgerPage() {
  const rows = await sqlRead`
    SELECT po.id, po.side, po.action, po.size_contracts, po.limit_price,
           po.hypothetical_fill_price, po.hypothetical_fill_size, po.status,
           po.reason, po.created_at,
           c.contract_id, c.domain, c.underlying
      FROM paper_orders po
      LEFT JOIN contracts c ON c.id = po.contract_pk
     WHERE po.created_at > NOW() - INTERVAL '24 hours'
     ORDER BY po.created_at DESC
     LIMIT 500
  `;
  return (
    <div>
      <h1 className="text-lg mb-3">paper ledger (last 24h)</h1>
      <table>
        <thead>
          <tr>
            <th>when</th>
            <th>contract</th>
            <th>domain</th>
            <th>side</th>
            <th>action</th>
            <th>size</th>
            <th>limit</th>
            <th>fill</th>
            <th>status</th>
            <th>reason</th>
          </tr>
        </thead>
        <tbody>
          {(rows as any[]).map((r) => (
            <tr key={r.id}>
              <td className="text-neutral-500">{fmtRelative(r.created_at)}</td>
              <td className="font-mono">{r.contract_id ?? "—"}</td>
              <td>{r.domain ?? "—"}</td>
              <td>{r.side}</td>
              <td>{r.action}</td>
              <td>{r.size_contracts}</td>
              <td>
                {r.limit_price != null ? `${(r.limit_price * 100).toFixed(1)}c` : "—"}
              </td>
              <td>
                {r.hypothetical_fill_price != null
                  ? `${(r.hypothetical_fill_price * 100).toFixed(1)}c x ${r.hypothetical_fill_size ?? 0}`
                  : "—"}
              </td>
              <td>{r.status}</td>
              <td className="text-neutral-500">{r.reason ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
