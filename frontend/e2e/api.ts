// A small HTTP client for the test's own setup steps: it plays the part of the chat turn that drafts
// the plan, and of the demo operator when a test does not drive the demo panel.

export const CUSTOMER = { handle: "demo-main", password: "wg-demo-main-2026" };
export const DEMO = { handle: "demo-panel", password: "wg-demo-panel-2026" };

const SEARCH = { sector: "technology", target_max_pct: "40.0000", fee_cap_fen: 2_000 };
const RESERVE_FEN = 500_000;
const RENT = "cmt_rent";
const INSURANCE = "cmt_insurance";
const CST_OFFSET_MS = 8 * 60 * 60 * 1000;

interface Json {
  [key: string]: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export class Api {
  private cookies = new Map<string, string>();
  private csrf = "";

  private readonly base: string;

  constructor(base: string) {
    this.base = base;
  }

  async login(handle: string, password: string): Promise<void> {
    const entry = await this.post("/v1/auth/login", { handle, password });
    this.csrf = entry["csrf_token"];
  }

  private async request(method: string, path: string, body?: unknown): Promise<Json> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.cookies.size > 0) {
      headers["Cookie"] = [...this.cookies].map(([name, value]) => `${name}=${value}`).join("; ");
    }
    if (method === "POST") headers["X-CSRF-Token"] = this.csrf;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${this.base}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    for (const line of response.headers.getSetCookie()) {
      const [pair] = line.split(";");
      const at = pair.indexOf("=");
      this.cookies.set(pair.slice(0, at), pair.slice(at + 1));
    }
    const text = await response.text();
    if (!response.ok) throw new Error(`${method} ${path} -> ${response.status}: ${text.slice(0, 300)}`);
    return text ? (JSON.parse(text) as Json) : {};
  }

  get = (path: string) => this.request("GET", path);
  post = (path: string, body?: unknown) => this.request("POST", path, body);

  async code(challengeId: string): Promise<string> {
    const { messages } = await this.get("/v1/sms");
    const found = (messages as Json[]).filter((m) => m["challenge_id"] === challengeId);
    if (found.length !== 1) throw new Error(`expected one SMS for ${challengeId}, found ${found.length}`);
    return found[0]["code"];
  }

  /** Ask for a code on a plan node and enter it, as the node's code box does. */
  async verifyNode(actionId: string): Promise<Json> {
    const challenge = await this.post(`/v1/actions/${actionId}/challenge`);
    const code = await this.code(challenge["challenge_id"]);
    return this.post(`/v1/challenges/${challenge["challenge_id"]}/verify`, { code });
  }

  async clock(): Promise<Json> {
    return this.post("/sim/clock/advance", { days: 0, minutes: 0 });
  }

  /** Move the simulated clock to 09:00 on T+day. */
  async advanceToDay(day: number): Promise<void> {
    const { epoch } = await this.clock();
    await this.post("/sim/clock/advance", { to: new Date(new Date(epoch).getTime() + day * 86_400_000).toISOString() });
  }

  /** The S6 plan (reserve, redemption, purchase, check, rent, insurance), drafted and joined but not approved. */
  async composePlan(): Promise<string> {
    const found = await this.post("/v1/funds/candidates", SEARCH);
    const funds = await this.post("/v1/funds/plans", { calc_result_id: found["calc_result_id"], with_bounds: true });
    const fundNodes = await this.get(`/v1/plans/${funds["plan_id"]}`);
    const byKind = (kind: string) =>
      (fundNodes["nodes"] as Json[]).find((node) => node["kind"] === kind)?.["logical_action_id"] as string;
    const reserve = await this.post("/v1/drafts/reserve", { target_fen: RESERVE_FEN });
    const rent = await this.post("/v1/drafts/pay-bill", { commitment_id: RENT });
    const insurance = await this.post("/v1/drafts/pay-bill", { commitment_id: INSURANCE });
    const redeem = byKind("REDEEM");
    const purchase = byKind("PURCHASE");
    const composed = await this.post("/v1/plans/compose", {
      draft_ids: [
        reserve["logical_action_id"],
        redeem,
        purchase,
        byKind("CHECK_OUTCOME"),
        rent["logical_action_id"],
        insurance["logical_action_id"],
      ],
      depends_on: { [purchase]: [redeem] },
    });
    return composed["plan_id"];
  }

  async approvePlan(planId: string): Promise<void> {
    const plan = await this.get(`/v1/plans/${planId}`);
    const card = await this.get(`/v1/cards/${plan["card_id"]}`);
    await this.post(`/v1/cards/${card["card_id"]}/confirm`, { terms_hash: card["terms_hash"] });
  }

  async nodeId(planId: string, kind: string, commitmentId?: string): Promise<string> {
    const plan = await this.get(`/v1/plans/${planId}`);
    const node = (plan["nodes"] as Json[]).find(
      (n) => n["kind"] === kind && (commitmentId === undefined || n["commitment_id"] === commitmentId),
    );
    if (!node) throw new Error(`no ${kind} node in ${planId}`);
    return node["logical_action_id"];
  }

  /** The plan approved, redeemed (T+0), the rent paid (T+2) and the redemption settled (T+3). */
  async planAtDay3(demo: Api): Promise<string> {
    const planId = await this.composePlan();
    await this.approvePlan(planId);
    await this.verifyNode(await this.nodeId(planId, "REDEEM"));
    await demo.advanceToDay(2);
    await this.verifyNode(await this.nodeId(planId, "PAY_BILL", RENT));
    await demo.advanceToDay(3);
    return planId;
  }
}

export function cst(iso: string): string {
  const shifted = new Date(new Date(iso).getTime() + CST_OFFSET_MS).toISOString();
  return `${shifted.slice(0, 10)} ${shifted.slice(11, 16)}`;
}
