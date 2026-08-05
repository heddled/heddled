"""A tool is one directory: schema + handler, testable in isolation.

    heddled tool test lookup_invoice --args '{"invoice_number":"F-2231"}'

`handle(args, ctx)` is the whole contract. `ctx` carries the session, the store
and `ctx.log(...)` for anything worth putting on the spine.
"""

# Stand-in for a real billing system. Replace the body, keep the signature.
INVOICES = {
    "F-2231": {"status": "unpaid", "amount_eur": 249.00, "customer": "Acme BV",
               "due": "2026-08-14"},
    "F-2232": {"status": "paid", "amount_eur": 89.50, "customer": "Acme BV",
               "due": "2026-07-30"},
    "F-2240": {"status": "overdue", "amount_eur": 1450.00, "customer": "Northwind NV",
               "due": "2026-07-01"},
}


def handle(args, ctx):
    number = str(args["invoice_number"]).strip().upper()
    ctx.log(f"looking up {number}")
    invoice = INVOICES.get(number)
    if not invoice:
        return {"status": "not_found", "amount_eur": 0,
                "message": f"No invoice {number} exists."}
    return {"invoice_number": number, **invoice}
