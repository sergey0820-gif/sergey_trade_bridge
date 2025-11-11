# inspect_positions.py

import os
from tinkoff.invest import Client
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")


def print_positions():
    with Client(TOKEN) as client:
        accounts = client.users.get_accounts()
        print(f"\n📑 Всего аккаунтов: {len(accounts.accounts)}")
        for acc in accounts.accounts:
            print(f"🔹 Тип: {acc.type}, ID: {acc.id}")
        print("\n📊 Аккаунт для анализа:", ACCOUNT_ID)

        response = client.operations.get_positions(account_id=ACCOUNT_ID)
        all_positions = (
            list(response.securities) + list(response.futures) + list(response.options)
        )

        if not all_positions:
            print("🚫 Нет открытых позиций.")
            return

        print(f"\n📦 Найдено {len(all_positions)} позиций:\n")
        for p in all_positions:
            figi = p.figi
            try:
                instrument = client.instruments.find_instrument(query=figi).instruments[
                    0
                ]
                ticker = instrument.ticker
                class_code = instrument.class_code
            except Exception as e:
                ticker = "UNKNOWN"
                class_code = "?"
            # Выбор поля объема
            if hasattr(p, "balance"):
                qty = p.balance
            elif hasattr(p, "quantity"):
                qty = float(p.quantity.units) + float(p.quantity.nano) / 1e9
            else:
                qty = 0

            # Цена
            if hasattr(p, "average_position_price"):
                price = (
                    float(p.average_position_price.units)
                    + float(p.average_position_price.nano) / 1e9
                )
            else:
                price = 0.0

            print(
                f"• Ticker: {ticker: <10} | FIGI: {figi} | Qty: {qty:.2f} | LotPrice: {price:.2f} | Class: {class_code}"
            )


if __name__ == "__main__":
    print("🔍 Получаем текущие позиции по счёту...\n")
    print_positions()
