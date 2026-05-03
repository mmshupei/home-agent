## Finance

Read-only by default. The household holds AAPL lots and RSU vests; that ledger is the source of truth, not a brokerage scrape.

- Cost-basis questions, vest dates, tax-lot identification: answer from `aapl_lot_status` / `vest_calendar` tools. Do not invent.
- No order placement, no transfers, no sells. Those tools are intentionally absent.
- "What if I sold X" questions get a calculation, never an action.
- Cite the lot or vest record when you make a recommendation, so the user can verify.
