# Payments are tracked after cooking, they don't block the order

Shops in the owner's village start cooking before customers pay. A customer may pay before, during or after cooking, or even on a later shop day. The bot therefore puts every confirmed order on the queue right away. Slip checking only records which orders are paid, and never decides whether an order gets cooked. The things built on this, such as the cross-day unpaid balance, slip matching against older orders, the "ค้างจ่าย" list and refund-due tracking, assume orders exist before they are paid.

## Considered Options

- **Pay before cooking (rejected):** safer against customers who never pay, but it breaks local trust norms, adds a wait to every order, and needs a held "awaiting payment" state.
- **Pay first only for delivery to first-time customers (deferred):** add it only after a real case of someone not paying.

## Consequences

- Some orders will go unpaid. The owner sees them on `/kitchen` and in `สรุปวันนี้` and follows up in person.
- A paid order can be cancelled. The app flags it as a refund due, and the owner returns the money by hand.
- The PromptPay QR codes have no expiry. A late payment for a real unpaid order is accepted, as long as the slip isn't older than the orders it pays.
