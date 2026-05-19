import { wrapTool } from "../../tool-logger.js";
const wrap = (def) => wrapTool(def, { plugin: "nbhd-finance-tools" });

/**
 * NBHD Finance Tools Plugin
 *
 * Budget tracking and debt payoff tools:
 * - Add/update accounts (debts and savings)
 * - List accounts with current balances
 * - Record payments and update balances
 * - Calculate and compare payoff strategies
 * - Get financial summary for context
 */

const DEFAULT_REQUEST_TIMEOUT_MS = 20000;

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asTrimmedString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function parseInteger(value, { defaultValue, min, max }) {
  if (value === undefined || value === null || value === "") return defaultValue;
  const parsed = Number.parseInt(String(value), 10);
  if (Number.isNaN(parsed)) return defaultValue;
  return Math.max(min, Math.min(max, parsed));
}

function getRuntimeConfig(api) {
  const pluginConfig = asObject(api.pluginConfig);
  const apiBaseUrl = asTrimmedString(
    pluginConfig.apiBaseUrl || process.env.NBHD_API_BASE_URL,
  ).replace(/\/+$/, "");
  const tenantId = asTrimmedString(process.env.NBHD_TENANT_ID);
  const internalKey = asTrimmedString(process.env.NBHD_INTERNAL_API_KEY);
  const requestTimeoutMs = parseInteger(pluginConfig.requestTimeoutMs, {
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
    min: 1000,
    max: 60000,
  });

  if (!apiBaseUrl) throw new Error("NBHD_API_BASE_URL is required");
  if (!tenantId) throw new Error("NBHD_TENANT_ID is required");
  if (!internalKey) throw new Error("NBHD_INTERNAL_API_KEY is required");

  return { apiBaseUrl, tenantId, internalKey, requestTimeoutMs };
}

function buildUrl(baseUrl, path, query) {
  const url = new URL(`${baseUrl}${path}`);
  for (const [key, value] of Object.entries(query || {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

function renderPayload(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: { json: payload },
  };
}

async function callRuntime(api, { path, method = "GET", query, body }) {
  const runtime = getRuntimeConfig(api);
  const url = buildUrl(runtime.apiBaseUrl, path, query);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), runtime.requestTimeoutMs);

  try {
    const headers = {
      "X-NBHD-Internal-Key": runtime.internalKey,
      "X-NBHD-Tenant-Id": runtime.tenantId,
    };
    let requestBody;
    if (method !== "GET" && body !== undefined) {
      headers["Content-Type"] = "application/json";
      requestBody = JSON.stringify(body);
    }

    const response = await fetch(url, {
      method,
      headers,
      body: requestBody,
      signal: controller.signal,
    });

    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = { raw };
      }
    }

    if (!response.ok) {
      const normalized = asObject(payload);
      const code = asTrimmedString(normalized.error) || "runtime_request_failed";
      const detail = asTrimmedString(normalized.detail);
      const detailSuffix = detail ? ` (${detail})` : "";
      throw new Error(`NBHD runtime error ${response.status}: ${code}${detailSuffix}`);
    }

    return asObject(payload);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(`NBHD runtime request timed out after ${runtime.requestTimeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function financePath(api, suffix) {
  const runtime = getRuntimeConfig(api);
  return `/api/v1/finance/runtime/${encodeURIComponent(runtime.tenantId)}${suffix}`;
}

export default function register(api) {
  // ── Add Account ──────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_add_account",
      description:
        "Add or update a financial account (debt or savings). If an account with the same nickname already exists, it will be updated. Use for tracking credit cards, loans, savings accounts, etc.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          nickname: {
            type: "string",
            description:
              'A short user-friendly name for the account, e.g. "Chase Card", "Car Loan", "Emergency Fund".',
          },
          account_type: {
            type: "string",
            enum: [
              "credit_card", "student_loan", "personal_loan", "mortgage",
              "auto_loan", "medical_debt", "other_debt",
              "savings", "checking", "emergency_fund",
            ],
            description: "Type of account.",
          },
          current_balance: {
            type: "number",
            description: "Current balance in dollars.",
          },
          interest_rate: {
            type: "number",
            description: "Annual percentage rate (APR). E.g. 22.9 for 22.9%.",
          },
          minimum_payment: {
            type: "number",
            description: "Monthly minimum payment in dollars.",
          },
          credit_limit: {
            type: "number",
            description: "Credit limit (for credit cards).",
          },
          due_day: {
            type: "integer",
            description: "Day of month payment is due (1-31).",
          },
        },
        required: ["nickname", "account_type", "current_balance"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          nickname: asTrimmedString(input.nickname),
          account_type: asTrimmedString(input.account_type) || "other_debt",
          current_balance: input.current_balance,
        };
        if (input.interest_rate !== undefined) body.interest_rate = input.interest_rate;
        if (input.minimum_payment !== undefined) body.minimum_payment = input.minimum_payment;
        if (input.credit_limit !== undefined) body.credit_limit = input.credit_limit;
        if (input.due_day !== undefined) body.due_day = input.due_day;

        const payload = await callRuntime(api, {
          path: financePath(api, "/accounts/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── List Accounts ────────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_list_accounts",
      description:
        "List financial accounts with current balances, interest rates, and payment info. By default returns only active accounts. Set archived_only=true to see accounts the user has archived (so they can restore one), or include_archived=true to see everything.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          archived_only: {
            type: "boolean",
            description:
              "When true, return only archived accounts instead of active ones. Use this to help the user find an account to restore.",
          },
          include_archived: {
            type: "boolean",
            description:
              "When true, return both active and archived accounts. Each row includes an is_active field. Ignored if archived_only is true.",
          },
        },
      },
      async execute(_id, params) {
        const input = asObject(params);
        const query = {};
        if (input.archived_only) {
          query.archived = "true";
        } else if (input.include_archived) {
          query.archived = "all";
        }
        const payload = await callRuntime(api, {
          path: financePath(api, "/accounts/"),
          method: "GET",
          query,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Record Payment ───────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_record_payment",
      description:
        "Record a payment toward an account. Automatically updates the account balance. Fuzzy-matches account by nickname.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          account_nickname: {
            type: "string",
            description: "Nickname of the account to pay (fuzzy matched).",
          },
          amount: {
            type: "number",
            description: "Payment amount in dollars.",
          },
          date: {
            type: "string",
            description: "Payment date in YYYY-MM-DD format. Defaults to today.",
          },
          transaction_type: {
            type: "string",
            enum: ["payment", "charge", "transfer", "refund", "interest"],
            description: "Type of transaction. Defaults to payment.",
          },
          description: {
            type: "string",
            description: "Optional note about the transaction.",
          },
        },
        required: ["account_nickname", "amount"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          account_nickname: asTrimmedString(input.account_nickname),
          amount: input.amount,
        };
        if (input.date) body.date = asTrimmedString(input.date);
        if (input.transaction_type) body.transaction_type = asTrimmedString(input.transaction_type);
        if (input.description) body.description = asTrimmedString(input.description);

        const payload = await callRuntime(api, {
          path: financePath(api, "/transactions/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Update Balance ───────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_update_balance",
      description:
        'Directly update an account\'s current balance. Use when the user reports a new statement balance, e.g. "my Chase card is now $3,800".',
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          account_nickname: {
            type: "string",
            description: "Nickname of the account (fuzzy matched).",
          },
          new_balance: {
            type: "number",
            description: "New current balance in dollars.",
          },
        },
        required: ["account_nickname", "new_balance"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: financePath(api, "/balance/"),
          method: "POST",
          body: {
            account_nickname: asTrimmedString(input.account_nickname),
            new_balance: input.new_balance,
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Archive Account ──────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_archive_account",
      description:
        "Archive a financial account. Hides it from the Gravity dashboard and removes it from debt totals and payoff calculations, while preserving the record and its transaction history. Use this when the user has a duplicate, stale, consolidated, or paid-off account they want out of their view. This is NOT a delete — the account can be restored later with nbhd_finance_unarchive_account. Fuzzy-matches account by nickname.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          account_nickname: {
            type: "string",
            description: "Nickname of the account to archive (fuzzy matched).",
          },
        },
        required: ["account_nickname"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: financePath(api, "/accounts/archive/"),
          method: "POST",
          body: {
            account_nickname: asTrimmedString(input.account_nickname),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Unarchive Account ────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_unarchive_account",
      description:
        "Restore a previously archived financial account. It will reappear on the Gravity dashboard and be included in totals and payoff calculations again. If unsure of the exact nickname, call nbhd_finance_list_accounts with archived_only=true first. Returns an error if an active account already exists with the same nickname — in that case, rename the active one first.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          account_nickname: {
            type: "string",
            description:
              "Nickname of the archived account to restore (fuzzy matched).",
          },
        },
        required: ["account_nickname"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const payload = await callRuntime(api, {
          path: financePath(api, "/accounts/unarchive/"),
          method: "POST",
          body: {
            account_nickname: asTrimmedString(input.account_nickname),
          },
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Calculate Payoff ─────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_calculate_payoff",
      description:
        "Calculate and compare debt payoff strategies. If no strategy is specified, compares all three: snowball (smallest balance first), avalanche (highest interest first), and hybrid. Returns timelines, total interest, and month-by-month schedules. IMPORTANT: When the user chooses or confirms a strategy, ALWAYS set save=true so the plan appears on their Gravity dashboard. Only omit save when doing an initial comparison.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          monthly_budget: {
            type: "number",
            description:
              "Total monthly amount available for all debt payments combined (including minimums).",
          },
          strategy: {
            type: "string",
            enum: ["snowball", "avalanche", "hybrid"],
            description:
              "Specific strategy to calculate. Omit to compare all three.",
          },
          save: {
            type: "boolean",
            description:
              "Save the result as the active payoff plan for the Gravity dashboard. ALWAYS set true when a specific strategy is chosen. Requires a specific strategy to be set.",
          },
        },
        required: ["monthly_budget"],
      },
      async execute(_id, params) {
        const input = asObject(params);
        const body = {
          monthly_budget: input.monthly_budget,
        };
        if (input.strategy) body.strategy = asTrimmedString(input.strategy);
        if (input.save) body.save = true;

        const payload = await callRuntime(api, {
          path: financePath(api, "/payoff/calculate/"),
          method: "POST",
          body,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Financial Summary ────────────────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_finance_summary",
      description:
        "Get a complete financial overview: total debt, total savings, all account details, active payoff plan, and monthly minimums. Prefer `nbhd_gravity_query` for any specific slice — this returns a fixed snapshot and is kept for backward compatibility.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: financePath(api, "/summary/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );

  // ── Parameterized Gravity query ──────────────────────────────────────
  api.registerTool(wrap({
      name: "nbhd_gravity_query",
      description:
        "Query the Gravity (finance) ledger for accounts, transactions, or active payoff plan. " +
        "USE THIS for any quantitative finance claim — debt totals, payment history, payoff progress, " +
        "due dates. Never state a finance number from memory or from USER.md alone; if you don't see a " +
        "number you need in this turn's tool results, query for it.\n\n" +
        "Parameters:\n" +
        "  • resource (required): one of \"accounts\", \"transactions\", \"plan\".\n" +
        "  • window: time range. Required for \"transactions\". Shape: {\"kind\": <enum>, \"value\": <if needed>}.\n" +
        "      Enum: today | yesterday | tomorrow | all | last_n_days (+value: int 1-730) | next_n_days |\n" +
        "      last_n_weeks (+value: int 1-104) | last_n_months (+value: int 1-24) | this_week | last_week |\n" +
        "      month_to_date | last_month | year_to_date | last_year | since (+value: \"YYYY-MM-DD\") |\n" +
        "      between (+value: [\"YYYY-MM-DD\", \"YYYY-MM-DD\"]).\n" +
        "  • filter: dict, resource-specific:\n" +
        "      accounts:     {is_active?: bool (default true), account_type?: str, nickname?: str (fuzzy), is_debt?: bool}\n" +
        "      transactions: {account_nickname?: str, account_id?: uuid, transaction_type?: str,\n" +
        "                     min_amount?: number, max_amount?: number}\n" +
        "      plan:         {is_active?: bool (default true), strategy?: \"snowball\"|\"avalanche\"|\"hybrid\"}\n" +
        "  • fields: optional list of field names; the identifier (id) is always included. Omit for all fields.\n" +
        "  • aggregate: optional one of \"sum\", \"count\", \"avg\", \"min\", \"max\". Requires aggregate_field for sum/avg/min/max.\n" +
        "  • aggregate_field: required if aggregate is sum/avg/min/max. e.g. \"amount\", \"current_balance\".\n" +
        "  • group_by: optional. transactions: \"transaction_type\"|\"account_id\"|\"account_nickname\"|\"date\". accounts: \"account_type\"|\"is_debt\".\n" +
        "  • order_by: optional. Prefix with \"-\" for descending. Defaults: transactions \"-date\", accounts \"nickname\", plan \"-created_at\".\n" +
        "  • limit: optional. Default 50, max 500. Response.meta.has_more is true if cap was reached.\n\n" +
        "Returns: {\"data\": [...], \"meta\": {schema_version, computed_at, tenant_tz, as_of, window_resolved_to: {from,to}, row_count, has_more, query_hash}}\n\n" +
        "All numeric amounts are returned as STRINGS to preserve Decimal precision; never parse as float.\n\n" +
        "Examples:\n" +
        "  Current debt total:\n" +
        "    {\"resource\": \"accounts\", \"filter\": {\"is_debt\": true}, \"aggregate\": \"sum\", \"aggregate_field\": \"current_balance\"}\n" +
        "  This week's payments:\n" +
        "    {\"resource\": \"transactions\", \"window\": {\"kind\": \"last_n_days\", \"value\": 7}, \"filter\": {\"transaction_type\": \"payment\"}}\n" +
        "  Monthly payment trend by account, last 6 months:\n" +
        "    {\"resource\": \"transactions\", \"window\": {\"kind\": \"last_n_months\", \"value\": 6}, \"filter\": {\"transaction_type\": \"payment\"}, \"aggregate\": \"sum\", \"aggregate_field\": \"amount\", \"group_by\": \"account_nickname\"}\n" +
        "  Current payoff plan:\n" +
        "    {\"resource\": \"plan\"}\n" +
        "  When did I last pay Student Loan AJ?\n" +
        "    {\"resource\": \"transactions\", \"window\": {\"kind\": \"all\"}, \"filter\": {\"account_nickname\": \"Student Loan AJ\"}, \"order_by\": \"-date\", \"limit\": 1}\n\n" +
        "GROUNDING CONTRACT: Any finance number you state to the user MUST come from a query result returned in this turn. Don't infer from USER.md. Don't recall. Don't extrapolate. If row_count is 0, say so plainly — never pad with stale figures.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          resource: { type: "string", enum: ["accounts", "transactions", "plan"] },
          window: {
            type: "object",
            additionalProperties: false,
            properties: {
              kind: {
                type: "string",
                enum: [
                  "today", "yesterday", "tomorrow", "all",
                  "last_n_days", "next_n_days", "last_n_weeks", "last_n_months",
                  "this_week", "last_week", "month_to_date", "last_month",
                  "year_to_date", "last_year", "since", "between",
                ],
              },
              value: {},
            },
            required: ["kind"],
          },
          filter: { type: "object", additionalProperties: true },
          fields: { type: "array", items: { type: "string" } },
          aggregate: { type: "string", enum: ["sum", "count", "avg", "min", "max"] },
          aggregate_field: { type: "string" },
          group_by: { type: "string" },
          order_by: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 500 },
        },
        required: ["resource"],
      },
      async execute(args) {
        const payload = await callRuntime(api, {
          path: financePath(api, "/query/"),
          method: "POST",
          body: args,
        });
        return renderPayload(payload);
      },
    }),
    { optional: true },
  );
}
