"""Deterministic sandbox policy. No model makes monetary decisions."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import re

# First-rung deposit for a validated but not yet trusted supplier: typical 30/70 T/T terms.
DEPOSIT_SHARE = Decimal('0.30')


class LocalRefusal(ValueError):
    """Refused by our own code before any request left the machine: nothing was sent."""


def money(value, *, zero=False):
    """Major units, at most two decimals; never binary floating point."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('amount must be decimal text, integer or Decimal')
    text = str(value)
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,2})?', text):
        raise ValueError('invalid or ambiguous major-unit amount')
    result = Decimal(text)
    if result < 0 or (result == 0 and not zero):
        raise ValueError('amount must be positive')
    return result.quantize(Decimal('.01'))


def currency(value):
    if not isinstance(value, str) or value not in ('EUR', 'GBP', 'CNY', 'USD'):
        raise ValueError('unsupported explicit currency')
    return value


def decide(amount, ccy, *, accepts_card, trusted, validated, loss_cap, balance, floor, check_floor=True):
    due, cap, ccy = money(amount), money(loss_cap), currency(ccy)
    available, minimum = money(balance, zero=True), money(floor, zero=True)
    if any(type(x) is not bool for x in (accepts_card, trusted, validated)):
        raise ValueError('policy signals must be booleans')
    exposure = (due * (Decimal('1.03') if ccy == 'CNY' else 1)).quantize(Decimal('.01'), rounding=ROUND_UP)
    if trusted:
        if not validated:
            raise ValueError('trusted supplier requires validated details')
        route, pay, exposure = 'TRANSFER', due, due
    elif accepts_card and exposure <= cap:
        route, pay = 'CARD', due
    elif validated:
        route = 'DEPOSIT'
        pay = min(cap, (due * DEPOSIT_SHARE).quantize(Decimal('.01'), rounding=ROUND_DOWN))
        if not pay:
            raise ValueError('deposit rounds to zero')
        exposure = pay
    else:
        raise ValueError('validated bank details or card within loss cap required')
    if check_floor and available - exposure < minimum:
        raise ValueError('currency balance floor would be breached')
    return {'route': route, 'amount': str(pay), 'cap': str(exposure), 'currency': ccy}
