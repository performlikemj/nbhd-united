import { LegalPage } from "@/components/legal-page";

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy Policy" lastUpdated="February 13, 2026">
      <h2>1. Data We Collect</h2>
      <p>We collect the following information when you use NBHD United:</p>
      <ul>
        <li>
          <strong>Account information</strong> — email address and display name
          provided during registration.
        </li>
        <li>
          <strong>Telegram chat ID</strong> — used to link your Telegram account
          and deliver AI assistant messages.
        </li>
        <li>
          <strong>Usage metrics</strong> — message counts and feature usage to
          monitor service health and enforce usage limits.
        </li>
        <li>
          <strong>Payment information</strong> — processed and stored by Stripe.
          We do not store your credit card details directly.
        </li>
      </ul>

      <h2>2. How We Use Your Data</h2>
      <ul>
        <li>To provide and maintain the AI assistant service.</li>
        <li>To process subscription payments through Stripe.</li>
        <li>
          To communicate with you about your account, billing, and service
          updates.
        </li>
        <li>To monitor and improve service quality and reliability.</li>
      </ul>

      <h2>3. Third-Party Sharing</h2>
      <p>
        We share data with the following third parties only as necessary to
        operate the service:
      </p>
      <ul>
        <li>
          <strong>Stripe</strong> — receives your email and payment details for
          subscription billing.
        </li>
        <li>
          <strong>Telegram</strong> — receives messages you send and delivers
          assistant responses.
        </li>
        <li>
          <strong>AI model providers</strong> (Anthropic, OpenRouter, and others)
          — receive your message content to generate AI responses. The specific
          provider depends on your subscription plan. Messages are processed
          according to each provider&apos;s usage policies.
        </li>
        <li>
          <strong>Microsoft Azure</strong> — hosts our infrastructure. Data is
          stored on Azure servers.
        </li>
      </ul>
      <p>
        We do not sell your personal data to advertisers or other third parties.
      </p>

      <h2>4. Data Retention</h2>
      <p>
        We retain your account data for the duration of your subscription. If
        you cancel your subscription and request account deletion, we will
        delete your personal data within 30 days, except where retention is
        required by law or for legitimate business purposes (e.g., billing
        records).
      </p>

      <h2>5. Cookies & Local Storage</h2>
      <p>
        We use browser local storage to store authentication tokens (JWT) for
        session management. We do not use tracking cookies, advertising cookies,
        or third-party analytics trackers.
      </p>

      <h2>6. Your Rights</h2>
      <p>You have the right to:</p>
      <ul>
        <li>
          <strong>Access</strong> your personal data — contact us to request a
          copy of the data we hold about you.
        </li>
        <li>
          <strong>Correct</strong> inaccurate data — update your display name
          and email through your account settings.
        </li>
        <li>
          <strong>Delete</strong> your data — request account deletion by
          contacting us at the email below.
        </li>
      </ul>

      <h2>7. Security</h2>
      <p>
        We use industry-standard security measures including encrypted
        connections (HTTPS/TLS), secure authentication tokens, and access
        controls. However, no method of transmission over the internet is 100%
        secure, and we can&apos;t guarantee absolute security.
      </p>

      <h2>8. Children&apos;s Privacy</h2>
      <p>
        NBHD United is not intended for use by anyone under the age of 13. We do
        not knowingly collect personal information from children under 13. If we
        become aware that we have collected data from a child under 13, we will
        take steps to delete it promptly.
      </p>

      <h2>9. Changes to This Policy</h2>
      <p>
        We may update this privacy policy from time to time. If we make material
        changes, we will notify you via email or through the service.
      </p>

      <h2>10. Contact</h2>
      <p>
        For privacy-related questions or requests, contact us at{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>.
      </p>
    </LegalPage>
  );
}
