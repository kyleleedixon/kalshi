import { NextRequest, NextResponse } from "next/server";
import { sqlWrite } from "@/lib/db";

// Node runtime (default). We intentionally do NOT run this on Edge — the
// engine, not Vercel, places orders; this route only writes the kill-switch
// flag. Keeping it on Node also keeps behavior consistent with the read paths.

const TOKEN = process.env.KILL_SWITCH_TOKEN;

export async function POST(req: NextRequest) {
  if (TOKEN) {
    const provided =
      req.headers.get("x-kill-switch-token") ??
      new URL(req.url).searchParams.get("token");
    if (provided !== TOKEN) {
      return new NextResponse("forbidden", { status: 403 });
    }
  }
  let body: { active?: boolean } = {};
  try {
    body = await req.json();
  } catch {
    return new NextResponse("bad json", { status: 400 });
  }
  if (typeof body.active !== "boolean") {
    return new NextResponse("missing `active` boolean", { status: 400 });
  }
  await sqlWrite`
    INSERT INTO control (key, value, updated_at, updated_by)
    VALUES ('kill_switch', ${{ active: body.active }}::jsonb, NOW(), 'dashboard')
    ON CONFLICT (key) DO UPDATE
      SET value = EXCLUDED.value,
          updated_at = NOW(),
          updated_by = 'dashboard'
  `;
  return NextResponse.json({ ok: true, active: body.active });
}
