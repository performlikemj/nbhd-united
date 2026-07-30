import { API_BASE, refreshAccessToken } from "@/lib/api";
import { getAccessToken, getAuthenticationEpoch } from "@/lib/auth";

const APPLE_SDK_ID = "apple-sign-in-sdk";
const APPLE_SDK_SRC =
  "https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js";

export const APPLE_CLIENT_ID =
  process.env.NEXT_PUBLIC_APPLE_CLIENT_ID ?? "";
export const APPLE_REDIRECT_URI =
  process.env.NEXT_PUBLIC_APPLE_REDIRECT_URI ?? "";

export const APPLE_TRANSACTION_TTL_SECONDS = 600;
const APPLE_TRANSACTION_REFRESH_MARGIN_SECONDS = 60;

export interface AppleAuthTransaction {
  transaction_id: string;
  state: string;
  nonce: string;
  expires_in?: number;
}

export interface AppleAuthorizationResponse {
  authorization: {
    code: string;
    state: string;
  };
}

interface AppleAuthApi {
  init(options: {
    clientId: string;
    scope: "email";
    redirectURI: string;
    state: string;
    nonce: string;
    usePopup: true;
  }): void;
  signIn(): Promise<AppleAuthorizationResponse>;
}

interface AppleIdSdk {
  auth: AppleAuthApi;
}

declare global {
  interface Window {
    AppleID?: AppleIdSdk;
  }
}

export interface PreparedAppleAuthorization {
  sdk: AppleIdSdk;
  transaction: AppleAuthTransaction;
  preparationStartedAt: number;
  refreshAt: number;
}

export interface AppleAuthenticationResult {
  access: string;
  refresh: string;
  created: boolean;
}

export type AppleAuthFlow = "authenticate" | "link";

export class AppleAuthFlowError extends Error {
  readonly kind: "http" | "network" | "popup";
  readonly status?: number;
  readonly errorCode?: string | null;

  constructor(args: {
    kind: "http" | "network" | "popup";
    status?: number;
    errorCode?: string | null;
  }) {
    super("Apple authentication failed");
    this.name = "AppleAuthFlowError";
    this.kind = args.kind;
    this.status = args.status;
    this.errorCode = args.errorCode;
  }
}

let sdkPromise: Promise<AppleIdSdk> | null = null;

/** Load Apple's CDN script once; a failed load is fully retryable. */
export function loadAppleSdk(): Promise<AppleIdSdk> {
  if (typeof window === "undefined") {
    return Promise.reject(new AppleAuthFlowError({ kind: "network" }));
  }
  if (window.AppleID?.auth) return Promise.resolve(window.AppleID);
  if (sdkPromise) return sdkPromise;

  const attempt = new Promise<AppleIdSdk>((resolve, reject) => {
    const staleScript = document.getElementById(APPLE_SDK_ID);
    staleScript?.remove();

    const script = document.createElement("script");
    script.id = APPLE_SDK_ID;
    script.src = APPLE_SDK_SRC;
    script.async = true;
    script.defer = true;

    script.onload = () => {
      if (window.AppleID?.auth) {
        resolve(window.AppleID);
        return;
      }
      reject(new AppleAuthFlowError({ kind: "network" }));
    };
    script.onerror = () => {
      reject(new AppleAuthFlowError({ kind: "network" }));
    };

    document.head.appendChild(script);
  });

  sdkPromise = attempt.catch((error: unknown) => {
    sdkPromise = null;
    document.getElementById(APPLE_SDK_ID)?.remove();
    throw error;
  });
  return sdkPromise;
}

async function beginAppleTransaction(): Promise<AppleAuthTransaction> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/auth/apple/begin/`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    throw new AppleAuthFlowError({ kind: "network" });
  }

  if (!response.ok) throw await responseError(response);

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AppleAuthFlowError({ kind: "http", status: response.status });
  }
  if (!isAppleAuthTransaction(body)) {
    throw new AppleAuthFlowError({ kind: "http", status: response.status });
  }
  return body;
}

/** Start SDK loading and begin-transaction fetching in the same turn. */
export async function prepareAppleAuthorization(): Promise<PreparedAppleAuthorization> {
  const preparationStartedAt = Date.now();
  const [sdk, transaction] = await Promise.all([
    loadAppleSdk(),
    beginAppleTransaction(),
  ]);

  const expiresIn =
    transaction.expires_in ?? APPLE_TRANSACTION_TTL_SECONDS;
  const refreshAt =
    preparationStartedAt +
    Math.max(0, expiresIn - APPLE_TRANSACTION_REFRESH_MARGIN_SECONDS) * 1000;

  return { sdk, transaction, preparationStartedAt, refreshAt };
}

/**
 * Freeze the winning prefetched transaction into the SDK's global state.
 * The component calls this only after rejecting stale concurrent preparations.
 */
export function initializeAppleAuthorization(
  prepared: PreparedAppleAuthorization,
): void {
  prepared.sdk.auth.init({
    clientId: APPLE_CLIENT_ID,
    scope: "email",
    redirectURI: APPLE_REDIRECT_URI,
    state: prepared.transaction.state,
    nonce: prepared.transaction.nonce,
    usePopup: true,
  });
}

/**
 * This function is intentionally synchronous up to the SDK call. Call it
 * directly from the click handler; `signIn` takes no arguments.
 */
export function activateApplePopup(
  prepared: PreparedAppleAuthorization,
): Promise<AppleAuthorizationResponse> {
  return prepared.sdk.auth.signIn();
}

export async function submitAppleAuthorization(args: {
  flow: "authenticate";
  prepared: PreparedAppleAuthorization;
  response: AppleAuthorizationResponse;
}): Promise<AppleAuthenticationResult>;
export async function submitAppleAuthorization(args: {
  flow: "link";
  prepared: PreparedAppleAuthorization;
  response: AppleAuthorizationResponse;
  currentPassword: string;
  bearer: string | null;
  authenticationEpoch: number;
}): Promise<{ linked: true; bearer: string | null }>;
export async function submitAppleAuthorization(args: {
  flow: AppleAuthFlow;
  prepared: PreparedAppleAuthorization;
  response: AppleAuthorizationResponse;
  currentPassword?: string;
  bearer?: string | null;
  authenticationEpoch?: number;
}): Promise<
  AppleAuthenticationResult | { linked: true; bearer: string | null }
> {
  const authorization = args.response?.authorization;
  if (
    !authorization ||
    typeof authorization.code !== "string" ||
    !authorization.code ||
    typeof authorization.state !== "string" ||
    !authorization.state
  ) {
    throw new AppleAuthFlowError({ kind: "popup" });
  }

  const link = args.flow === "link";
  const requestBody = link
    ? {
        transaction_id: args.prepared.transaction.transaction_id,
        code: authorization.code,
        state: authorization.state,
        current_password: args.currentPassword ?? "",
      }
    : {
        transaction_id: args.prepared.transaction.transaction_id,
        code: authorization.code,
        state: authorization.state,
      };
  const serializedRequestBody = JSON.stringify(requestBody);

  let response: Response;
  let linkBearer = args.bearer ?? null;
  if (link) {
    const authenticationEpoch = args.authenticationEpoch;
    if (typeof authenticationEpoch !== "number") {
      throw new AppleAuthFlowError({ kind: "popup" });
    }
    assertLinkAttemptCurrent(authenticationEpoch, linkBearer);
    response = await postAppleAuthorization(
      "link",
      serializedRequestBody,
      linkBearer,
    );
    assertLinkAttemptCurrent(authenticationEpoch, linkBearer);

    if (response.status === 401) {
      // DRF authenticates before parsing, so this 401 consumed neither the
      // transaction nor the code. Refresh once and retry this identical body.
      linkBearer = await refreshAccessToken({
        authenticationEpoch,
        accessToken: linkBearer,
      });
      assertLinkAttemptCurrent(authenticationEpoch, linkBearer);
      response = await postAppleAuthorization(
        "link",
        serializedRequestBody,
        linkBearer,
      );
      assertLinkAttemptCurrent(authenticationEpoch, linkBearer);
    }
  } else {
    response = await postAppleAuthorization(
      "complete",
      serializedRequestBody,
      null,
    );
  }

  if (!response.ok) throw await responseError(response);

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new AppleAuthFlowError({ kind: "http", status: response.status });
  }

  if (link) {
    assertLinkAttemptCurrent(
      args.authenticationEpoch as number,
      linkBearer,
    );
    if (!isRecord(body) || body.linked !== true) {
      throw new AppleAuthFlowError({ kind: "http", status: response.status });
    }
    return { linked: true, bearer: linkBearer };
  }

  if (
    !isRecord(body) ||
    typeof body.access !== "string" ||
    !body.access ||
    typeof body.refresh !== "string" ||
    !body.refresh ||
    typeof body.created !== "boolean"
  ) {
    throw new AppleAuthFlowError({ kind: "http", status: response.status });
  }
  return {
    access: body.access,
    refresh: body.refresh,
    created: body.created,
  };
}

export function normalizeAppleFailure(error: unknown): AppleAuthFlowError {
  if (error instanceof AppleAuthFlowError) return error;
  return new AppleAuthFlowError({ kind: "popup" });
}

async function responseError(response: Response): Promise<AppleAuthFlowError> {
  let errorCode: string | null = null;
  try {
    const body = (await response.json()) as unknown;
    if (isRecord(body) && typeof body.error === "string") {
      errorCode = body.error;
    }
  } catch {
    // Status alone still maps 429/503 correctly; other shapes stay generic.
  }
  return new AppleAuthFlowError({
    kind: "http",
    status: response.status,
    errorCode,
  });
}

function isAppleAuthTransaction(value: unknown): value is AppleAuthTransaction {
  return (
    isRecord(value) &&
    typeof value.transaction_id === "string" &&
    value.transaction_id.length > 0 &&
    typeof value.state === "string" &&
    value.state.length > 0 &&
    typeof value.nonce === "string" &&
    value.nonce.length > 0 &&
    (!("expires_in" in value) ||
      (typeof value.expires_in === "number" &&
        Number.isFinite(value.expires_in) &&
        value.expires_in > 0))
  );
}

async function postAppleAuthorization(
  endpoint: "complete" | "link",
  body: string,
  bearer: string | null,
): Promise<Response> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (bearer) headers.authorization = `Bearer ${bearer}`;

  try {
    return await fetch(`${API_BASE}/api/v1/auth/apple/${endpoint}/`, {
      method: "POST",
      headers,
      body,
    });
  } catch {
    throw new AppleAuthFlowError({ kind: "network" });
  }
}

function assertLinkAttemptCurrent(
  expectedEpoch: number,
  expectedBearer: string | null,
): void {
  if (
    getAccessToken() !== expectedBearer ||
    getAuthenticationEpoch() !== expectedEpoch
  ) {
    throw new AppleAuthFlowError({ kind: "popup" });
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
