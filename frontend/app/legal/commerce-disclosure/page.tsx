import { LegalPage } from "@/components/legal-page";

export default function CommerceDisclosurePage() {
  return (
    <LegalPage
      title="Commerce Disclosure"
      lastUpdated="February 20, 2026"
    >
      <p className="text-ink-faint italic">
        特定商取引法に基づく表記
      </p>

      <h2>Legal Name (販売業者名)</h2>
      <p>Will be disclosed without delay upon request.</p>

      <h2>Head of Operations (運営責任者)</h2>
      <p>Will be disclosed without delay upon request.</p>

      <h2>Address (所在地)</h2>
      <p>Will be disclosed without delay upon request.</p>

      <h2>Phone Number (電話番号)</h2>
      <p>Will be disclosed without delay upon request.</p>
      <p className="text-sm text-ink-faint">
        Hours: 10:00 – 18:00 JST (excluding weekends and holidays)
      </p>

      <h2>Email Address (メールアドレス)</h2>
      <p>
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>
      </p>

      <h2>Price (販売価格)</h2>
      <ul>
        <li>Starter Plan: $12/month (USD)</li>
        <li>Premium Plan: $25/month (USD)</li>
        <li>Bring Your Own Key (BYOK) Plan: $8/month (USD)</li>
      </ul>
      <p>All prices include applicable taxes. Prices are displayed on each plan page.</p>

      <h2>Additional Fees (商品代金以外の必要料金)</h2>
      <p>
        There are no additional fees beyond the subscription price. Internet connection
        costs required to use the service are borne by the customer.
      </p>

      <h2>Accepted Payment Methods (支払方法)</h2>
      <p>Credit card payments via Stripe.</p>

      <h2>Payment Period (支払時期)</h2>
      <p>
        Credit card payments are processed immediately upon subscription. Recurring
        charges are billed automatically at the start of each billing cycle.
      </p>

      <h2>Service Delivery (引渡時期)</h2>
      <p>
        Access to the service is provided immediately upon successful payment and
        account setup.
      </p>

      <h2>Returns, Exchanges &amp; Refunds (返品・交換について)</h2>
      <h3>Cancellation by Customer</h3>
      <p>
        You may cancel your subscription at any time from your account settings or by
        contacting support. Upon cancellation, you retain access until the end of your
        current billing period. No partial refunds are issued for unused portions of a
        billing period.
      </p>
      <h3>Service Defects</h3>
      <p>
        If the service is unavailable or materially defective due to issues on our end,
        please contact{" "}
        <a href="mailto:mj@bywayofmj.com">mj@bywayofmj.com</a>.
        We will investigate and may issue a credit or refund at our discretion.
      </p>

      <h2>Operating Environment (動作環境)</h2>
      <p>
        Neighborhood United is a web-based service accessible via modern web browsers
        (Chrome, Safari, Firefox, Edge). The Telegram messaging app is required for
        AI assistant communication.
      </p>
    </LegalPage>
  );
}
