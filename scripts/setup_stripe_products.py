"""
One-time setup: creates the 3 Stripe Products + monthly recurring Prices
(Starter $10, Growth $22, Business $105) that the app's checkout flow needs
(app/services/stripe_service.py maps STRIPE_PRICE_* env vars to these).

Idempotent — each Price gets a stable `lookup_key`; re-running finds the
existing Price instead of creating a duplicate, so it's safe to run again
after an interrupted first run.

Run once, manually, after STRIPE_SECRET_KEY is in Backend/.env:
    cd Backend && python -m scripts.setup_stripe_products

Prints the 3 resulting Price IDs — paste them into .env as
STRIPE_PRICE_STARTER / STRIPE_PRICE_GROWTH / STRIPE_PRICE_BUSINESS.
"""

import logging

import stripe

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

TIERS = [
    {
        "lookup_key": "nexus_starter_monthly",
        "product_name": "Nexus Starter",
        "product_description": "25 documents, 20MB uploads, 500 messages/month.",
        "amount_cents": 1000,
    },
    {
        "lookup_key": "nexus_growth_monthly",
        "product_name": "Nexus Growth",
        "product_description": "150 documents, 50MB uploads, 5,000 messages/month.",
        "amount_cents": 2200,
    },
    {
        "lookup_key": "nexus_business_monthly",
        "product_name": "Nexus Business",
        "product_description": "1,000 documents, 100MB uploads, 12,000 messages/month.",
        "amount_cents": 10500,
    },
]

def main() -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise SystemExit("STRIPE_SECRET_KEY is not set in Backend/.env — add it first.")
    stripe.api_key = settings.STRIPE_SECRET_KEY

    results: dict[str, str] = {}

    for tier in TIERS:
        existing = stripe.Price.list(lookup_keys=[tier["lookup_key"]], active=True, limit=1)
        if existing.data:
            price = existing.data[0]
            logger.info("%s already exists: %s", tier["product_name"], price.id)
        else:
            product = stripe.Product.create(
                name=tier["product_name"],
                description=tier["product_description"],
            )
            price = stripe.Price.create(
                product=product.id,
                unit_amount=tier["amount_cents"],
                currency="usd",
                recurring={"interval": "month"},
                lookup_key=tier["lookup_key"],
            )
            logger.info("Created %s: %s", tier["product_name"], price.id)
        results[tier["lookup_key"]] = price.id

    print("\nPaste these into Backend/.env:\n")
    print(f"STRIPE_PRICE_STARTER={results['nexus_starter_monthly']}")
    print(f"STRIPE_PRICE_GROWTH={results['nexus_growth_monthly']}")
    print(f"STRIPE_PRICE_BUSINESS={results['nexus_business_monthly']}")

if __name__ == "__main__":
    main()
