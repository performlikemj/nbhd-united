// Spec for the web→app handoff decision logic. Pure functions only — runnable
// with Node's built-in runner (`node --test`) after a tsc transpile, no test
// framework dependency required. See docs/web-signup-account-confirmation-flow.md.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  authPathForIntent,
  decideAfterProbe,
  decideInitialStep,
  stepForDifferentAccount,
} from "./authorize-decision";

test("authPathForIntent maps intents; unknown falls back to signup", () => {
  assert.equal(authPathForIntent("signin"), "/login");
  assert.equal(authPathForIntent("register"), "/signup");
  assert.equal(authPathForIntent("garbage"), "/signup");
});

test("S1: first hop, register, not logged in → fresh signup (no clear)", () => {
  assert.deepEqual(
    decideInitialStep({ firstHop: true, isLoggedIn: false, intent: "register" }),
    { kind: "redirect-auth", target: "/signup", clearFirst: false },
  );
});

test("first hop, signin, not logged in → login (no clear)", () => {
  assert.deepEqual(
    decideInitialStep({ firstHop: true, isLoggedIn: false, intent: "signin" }),
    { kind: "redirect-auth", target: "/login", clearFirst: false },
  );
});

test("S2/S3: first hop + leftover session → probe (both intents)", () => {
  assert.deepEqual(
    decideInitialStep({ firstHop: true, isLoggedIn: true, intent: "register" }),
    { kind: "probe-identity" },
  );
  assert.deepEqual(
    decideInitialStep({ firstHop: true, isLoggedIn: true, intent: "signin" }),
    { kind: "probe-identity" },
  );
});

test("bounce-back + logged in → finish (the ONLY auto-complete path)", () => {
  assert.deepEqual(
    decideInitialStep({ firstHop: false, isLoggedIn: true, intent: "register" }),
    { kind: "finish" },
  );
  assert.deepEqual(
    decideInitialStep({ firstHop: false, isLoggedIn: true, intent: "signin" }),
    { kind: "finish" },
  );
});

test("bounce-back + not logged in → auth UI, defensive, no clear", () => {
  assert.deepEqual(
    decideInitialStep({ firstHop: false, isLoggedIn: false, intent: "register" }),
    { kind: "redirect-auth", target: "/signup", clearFirst: false },
  );
});

test("S2: probe resolves identity → choose-account carries the email", () => {
  assert.deepEqual(decideAfterProbe({ email: "jane@example.com" }, "register"), {
    kind: "choose-account",
    email: "jane@example.com",
  });
});

test("S4: probe returns null (dead session) → clear + auth UI, by intent", () => {
  assert.deepEqual(decideAfterProbe(null, "register"), {
    kind: "redirect-auth",
    target: "/signup",
    clearFirst: true,
  });
  assert.deepEqual(decideAfterProbe(null, "signin"), {
    kind: "redirect-auth",
    target: "/login",
    clearFirst: true,
  });
});

test("D2: 'use a different account' clears first, routes by intent", () => {
  assert.deepEqual(stepForDifferentAccount("register"), {
    kind: "redirect-auth",
    target: "/signup",
    clearFirst: true,
  });
  assert.deepEqual(stepForDifferentAccount("signin"), {
    kind: "redirect-auth",
    target: "/login",
    clearFirst: true,
  });
});

// The core invariant that fixes the bug: a first-hop register/sign-in with a
// leftover session must NEVER resolve straight to "finish". It must go through
// the user's explicit choice (via probe → choose-account).
test("INVARIANT: first-hop + leftover session never auto-finishes", () => {
  for (const intent of ["register", "signin"]) {
    const step = decideInitialStep({ firstHop: true, isLoggedIn: true, intent });
    assert.notEqual(step.kind, "finish");
    assert.equal(step.kind, "probe-identity");
  }
});
