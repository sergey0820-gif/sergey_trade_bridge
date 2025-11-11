import asyncio
from dotenv import dotenv_values
from tinkoff.invest import AsyncClient, OrderDirection, OrderType
from trade_utils.orders import post_order_safe


async def main():
    cfg = dotenv_values(".env")
    async with AsyncClient(cfg["TINKOFF_TOKEN"]) as c:
        res = await post_order_safe(
            c,
            account_id=cfg["TINKOFF_ACCOUNT_ID"],
            ticker="SBER",
            class_code="TQBR",
            qty_lots=1,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_type=OrderType.ORDER_TYPE_MARKET,
            dry_run=True,  # 🔒 только проверки, без фактического постинга
        )
        print("DRY-RUN OK:", res)


if __name__ == "__main__":
    asyncio.run(main())
