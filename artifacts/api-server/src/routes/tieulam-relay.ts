import { Router, type IRouter } from "express";

const router: IRouter = Router();

const TIEULAM_FRONTEND = "https://sv1.tieulam1.live";
const TIEULAM_API_BASE = "https://api.tlap12062026.xyz";
const RELAY_SECRET = process.env["RELAY_SECRET"] ?? "";

const HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  Accept: "application/json, text/plain, */*",
  "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
  "Content-Type": "application/json",
  Referer: TIEULAM_FRONTEND + "/",
  Origin: TIEULAM_FRONTEND,
};

const MS = 7200 * 1000;

function cutoff(offsetMs: number) {
  return new Date(Date.now() + offsetMs)
    .toISOString()
    .replace("T", "T")
    .slice(0, 19);
}

router.get("/tieulam-relay", async (req, res) => {
  if (RELAY_SECRET) {
    const token =
      (req.headers["x-relay-token"] as string) ||
      (req.query["token"] as string);
    if (token !== RELAY_SECRET) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
  }

  const payload = {
    queries: [
      { field: "start_date", type: "gte", value: cutoff(-MS) },
      { field: "start_date", type: "lte", value: cutoff(36 * 3600 * 1000) },
    ],
    query_and: true,
    limit: 100,
    page: 1,
    order_asc: "start_date",
  };

  try {
    const upstream = await fetch(`${TIEULAM_API_BASE}/matches/graph`, {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(15000),
    });

    if (!upstream.ok) {
      res.status(502).json({ error: `Upstream ${upstream.status}` });
      return;
    }

    const data = await upstream.json();
    res.json({ data: (data as { data?: unknown[] }).data ?? [] });
  } catch (err) {
    req.log.error({ err }, "TieuLam relay fetch failed");
    res.status(502).json({ error: "Upstream fetch failed" });
  }
});

export default router;
