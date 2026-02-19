import { LegalPage } from "@/components/legal-page";

export default function RefundPage() {
  return (
    <LegalPage
      title="Refund & Cancellation Policy"
      lastUpdated="February 13, 2026"
    >
      <h2>1. Cancellation</h2>
      <p>
        You may cancel your Neighborhood United subscription at any time. When you
        cancel, your access to the AI assistant continues until the end of your
        current billing period. After that, your subscription will not renew and
        access will end.
      </p>

      <h2>2. How to Cancel</h2>
      <p>You can cancel your subscription in two ways:</p>
      <ul>
        <li>
          <strong>Via the Billing page</strong> — click &ldquo;Open Stripe Portal&rdquo; and
          manage your subscription from the Stripe customer portal.
        </li>
        <li>
          <strong>Via email</strong> — send a cancellation request to{" "}
          <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a> and we will
          process it within 1 business day.
        </li>
      </ul>

      <h2>3. Refunds</h2>
      <p>
        Neighborhood United subscriptions are generally non-refundable. Cancellation
        stops future charges but does not generate a prorated refund for the
        remaining days in your current billing period.
      </p>

      <h2>4. 48-Hour Exception</h2>
      <p>
        If you subscribed within the last 48 hours and have not used the AI
        assistant (no messages sent via Telegram), you may request a full refund
        by emailing{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>. We will
        review your request and process the refund within 5 business days if
        eligible.
      </p>

      <h2>5. Billing Disputes</h2>
      <p>
        If you believe you have been charged in error, please contact us at{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a> before filing a
        dispute with your bank or credit card provider. We will work with you to
        resolve any billing issues promptly.
      </p>

      <h2>6. Contact</h2>
      <p>
        For questions about cancellations, refunds, or billing, contact us at{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>.
      </p>
    </LegalPage>
  );
}
