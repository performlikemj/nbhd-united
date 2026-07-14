export type MessagingChannel = "telegram" | "line";

export interface PairingLink {
  deep_link: string;
  qr_code: string;
  expires_at: string;
}
