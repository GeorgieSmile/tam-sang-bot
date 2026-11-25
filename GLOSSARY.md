# tam-sang-bot

A LINE ordering assistant for one Thai made-to-order (ตามสั่ง) shop. The bot answers customers and takes orders. The owner runs the kitchen and steps in when the bot hands a conversation over.

## Ordering

**Shop day**:
The calendar date in `Asia/Bangkok` that orders, payments and hand-overs belong to. It resets at local midnight.
_Avoid_: Today, business day

**Order card**:
The priced summary the bot shows the customer before they confirm. It is not an order yet.
_Avoid_: Draft card, quote, cart

**Order**:
A customer purchase that exists once the customer confirms an order card. It is identified by its queue number for the shop day.
_Avoid_: Ticket, purchase

**Queue**:
The orders still waiting to be cooked, oldest first.
_Avoid_: Backlog, list

**Queue number**:
The `#N` given to an order in sequence within its shop day. It never changes.
_Avoid_: Order ID, ticket number

**Queue position**:
An order's place among the orders still in the queue: the number of orders ahead of it, plus one. It shrinks as earlier orders finish.
_Avoid_: Queue number, rank

**Meal box**:
One portion of a meal-category dish. Sides and drinks are not meal boxes.
_Avoid_: Item, plate, dish count

**Wait estimate**:
The rough time until a customer's food is ready, based on the meal boxes in the queue and in their order. It is never less than the owner's minimum wait.
_Avoid_: ETA, cook time

**Minimum wait**:
The lowest wait estimate the owner allows for the shop day, set with busy mode. It expires at midnight.
_Avoid_: Busy wait, fixed wait

## Payment

**Payment status**:
Whether an order is unpaid, paid by slip, or paid in cash. It is separate from whether the order is cooked.
_Avoid_: Paid flag, order status

**Unpaid balance**:
The sum of all of a customer's unpaid, non-cancelled orders, whichever shop day they were placed on.
_Avoid_: Bill, tab, debt

**Slip**:
An image of a bank transfer receipt that a customer sends as proof of payment.
_Avoid_: Receipt, proof

**Slip match**:
Assigning a slip's amount to that customer's unpaid orders, oldest first and then any open order card, whose totals add up to exactly that amount. A slip is never split, and several slips are never added together.
_Avoid_: Reconciliation, allocation

**Refund due**:
A cancelled order the customer had already paid for. The owner returns the money by hand, outside the app.
_Avoid_: Credit, chargeback, store credit

## Bot control

**Hand-over**:
One customer's conversation passed from the bot to the owner. The bot starts it when it should not or cannot answer, and the owner starts it to talk to the customer directly.
_Avoid_: Escalation, handoff, transfer

**Bot quiet**:
The state during a hand-over in which the bot sends nothing to that customer.
_Avoid_: Muted, paused, silent mode

**Bot paused**:
The whole-shop state in which the bot answers no customer at all.
_Avoid_: Bot off, disabled, quiet

**Inbox card**:
The entry on the kitchen board that shows the owner a hand-over still to be dealt with.
_Avoid_: Ticket, alert, notification
