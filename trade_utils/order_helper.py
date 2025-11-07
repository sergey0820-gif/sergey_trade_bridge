import logging
from tinkoff.invest import StopOrderDirection, StopOrderExpirationType, StopOrderType, PostStopOrderRequest, Quotation

def float_to_quotation(value: float) -> Quotation:
    units = int(value)
    nano = int((value - units) * 1e9)
    return Quotation(units=units, nano=nano)

def place_stop_order(client, account_id, instrument_uid, quantity, stop_price, direction, stop_order_type, order_type):
    try:
        stop_order = PostStopOrderRequest(
            quantity=quantity,
            price=float_to_quotation(0),  # Рыночная заявка — цена = 0
            stop_price=float_to_quotation(stop_price),
            direction=direction,
            stop_order_type=stop_order_type,
            order_type=order_type,
            instrument_id=instrument_uid,
            expire_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GTC,
            account_id=account_id
        )
        response = client.stop_orders.post_stop_order(stop_order)
        logging.info(f"✅ Заявка размещена: {stop_order_type.name} для {instrument_uid}, цена триггера: {stop_price}")
        return response
    except Exception as e:
        logging.error(f"🚨 Ошибка при размещении заявки {stop_order_type.name} для {instrument_uid}: {e}")
        return None

