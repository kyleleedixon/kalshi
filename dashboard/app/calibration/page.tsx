import { sqlRead } from "@/lib/db";
import { fmtPct, fmtRelative } from "@/lib/format";

export const dynamic = "force-dynamic";

interface Band {
  domain: string;
  band_lo: number;
  band_hi: number;
  settled_count: number;
  empirical_freq: number;
  mean_predicted: number;
  brier: number;
  brier_vs_uncorrected: number;
}

export default async function CalibrationPage() {
  const latest = await sqlRead`
    SELECT id, generated_at, phase_gate_min_sample, bands
      FROM calibration_snapshots
     ORDER BY generated_at DESC LIMIT 1
  `;
  const row = (latest as any[])[0];
  if (!row) {
    return (
      <div>
        <h1 className="text-lg mb-3">calibration</h1>
        <p className="text-neutral-400">
          No calibration snapshots yet. All bands are gated (fail-closed) until
          the calibration store accumulates enough settled sample.
        </p>
      </div>
    );
  }
  const bands: Band[] = row.bands as Band[];
  const byDomain = new Map<string, Band[]>();
  for (const b of bands) {
    if (!byDomain.has(b.domain)) byDomain.set(b.domain, []);
    byDomain.get(b.domain)!.push(b);
  }
  const minSample = row.phase_gate_min_sample as number;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg">calibration</h1>
        <p className="text-xs text-neutral-500">
          Snapshot {fmtRelative(row.generated_at)} · gate min sample {minSample}
        </p>
      </div>
      {[...byDomain.entries()].map(([domain, rows]) => (
        <section key={domain}>
          <h2 className="text-md text-neutral-300">{domain}</h2>
          <table>
            <thead>
              <tr>
                <th>band</th>
                <th>n</th>
                <th>empirical</th>
                <th>predicted</th>
                <th>brier</th>
                <th>brier improvement</th>
                <th>gated?</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .sort((a, b) => a.band_lo - b.band_lo)
                .map((b) => {
                  const improvement = -b.brier_vs_uncorrected;
                  const gated = b.settled_count < minSample;
                  return (
                    <tr key={`${domain}-${b.band_lo}`}>
                      <td>
                        {fmtPct(b.band_lo, 0)}–{fmtPct(b.band_hi, 0)}
                      </td>
                      <td>{b.settled_count}</td>
                      <td>{fmtPct(b.empirical_freq)}</td>
                      <td>{fmtPct(b.mean_predicted)}</td>
                      <td>{b.brier.toFixed(4)}</td>
                      <td
                        className={
                          improvement > 0 ? "text-green-400" : "text-red-400"
                        }
                      >
                        {(improvement * 1000).toFixed(1)} mbp
                      </td>
                      <td className={gated ? "text-red-400" : "text-green-400"}>
                        {gated ? "GATED" : "ok"}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
