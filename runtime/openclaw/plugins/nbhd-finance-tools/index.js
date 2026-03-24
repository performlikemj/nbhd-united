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
  api.registerTool(
    {
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
    },
    { optional: true },
  );

  // ── List Accounts ────────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_finance_list_accounts",
      description:
        "List all active financial accounts with current balances, interest rates, and payment info.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        const payload = await callRuntime(api, {
          path: financePath(api, "/accounts/"),
          method: "GET",
        });
        return renderPayload(payload);
      },
    },
    { optional: true },
  );

  // ── Record Payment ───────────────────────────────────────────────────
  api.registerTool(
    {
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
    },
    { optional: true },
  );

  // ── Update Balance ───────────────────────────────────────────────────
  api.registerTool(
    {
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
    },
    { optional: true },
  );

  // ── Calculate Payoff ─────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_finance_calculate_payoff",
      description:
        "Calculate and compare debt payoff strategies. If no strategy is specified, compares all three: snowball (smallest balance first), avalanche (highest interest first), and hybrid. Returns timelines, total interest, and month-by-month schedules.",
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
              "Set to true to save the result as the active payoff plan (requires a specific strategy).",
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
    },
    { optional: true },
  );

  // ── Financial Summary ────────────────────────────────────────────────
  api.registerTool(
    {
      name: "nbhd_finance_summary",
      description:
        "Get a complete financial overview: total debt, total savings, all account details, active payoff plan, and monthly minimums. Use this at the start of financial conversations for full context.",
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
    },
    { optional: true },
  );
}
