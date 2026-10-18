"""Shared commercial values for authored product copy and public templates."""
import re
from app import plans
from app.config import settings


def plan(key):
    return next(p for p in plans.CATALOG if p['key']==key and p.get('public'))


def price(key):
    return plan(key)['price']


def value(key):
    if key=='addon.mailbox': return plans.EXTRA_MAILBOX['price']
    if key=='addon.allowance':
        from app.usage import EXTRA_MAILBOX_EMAILS
        return str(EXTRA_MAILBOX_EMAILS)
    if key=='trial.days': return str(settings.trial_days)
    if key=='trial.cap': return str(settings.trial_email_cap)
    category, identity=key.split('.',1)
    fields={'price':'price','allowance':'emails','mailboxes':'included_mailboxes','name':'name'}
    return str(plan(identity)[fields[category]])


def public_plans():
    return [p for p in plans.CATALOG if p.get('public')]


def price_number(key):
    return price(key).removeprefix('£')


def subscription_price(key, extra=0):
    from decimal import Decimal
    amount=Decimal(price_number(key))+Decimal(plans.EXTRA_MAILBOX['price'].removeprefix('£'))*extra
    return f'£{amount:,.2f}'


def commission(key, months=1, count=1, extra=0):
    from decimal import Decimal
    from app.partners import DEFAULT_COMMISSION_RATE
    amount=Decimal(subscription_price(key,extra).removeprefix('£').replace(',',''))*Decimal(str(DEFAULT_COMMISSION_RATE))*months*count
    return f'£{amount:,.2f}'


def resolve(text):
    return re.sub(r'\{\{product:([a-z.]+)\}\}',lambda m:value(m[1]),text)


def resolve_data(data):
    if isinstance(data,str): return resolve(data)
    if isinstance(data,list): return [resolve_data(x) for x in data]
    if isinstance(data,tuple): return tuple(resolve_data(x) for x in data)
    if isinstance(data,dict): return {k:resolve_data(v) for k,v in data.items()}
    return data


def pricing_copy():
    labels={'scheduling':'scheduling','crm':'self-filling CRM','concierge':'Concierge with typed and live voice assistance'}
    lines=[f'Pricing: a {settings.trial_days}-day free trial with no card to start, ending at '
           f'{settings.trial_days} days or {settings.trial_email_cap} handled emails. Trial access does not enable disabled or restricted features.']
    for p in plans.CATALOG:
        if not p.get('public'):continue
        modules=plans.BASE_PLAN_MODULES.get(p['key'],set())
        included=', '.join(labels.get(m,m) for m in sorted(modules))
        lines.append(f"{p['name']} ({p['price']} {p['per']}): {p['emails']}; {p['included_mailboxes']} mailbox(es). Includes email and {included}.")
    lines.append(f"Extra mailbox: {plans.EXTRA_MAILBOX['price']} {plans.EXTRA_MAILBOX['per']}. See Plans and billing for the current allowance.")
    lines.append('Draft-first by default. Auto-send is opt-in. CRM access and Concierge access follow module entitlements; App help remains separate. There is no free-forever tier. Restricted operational modules are not on public sale; contact hello@blam8.com for larger requirements.')
    return '\n'.join(lines)


def public_capabilities():
    from app import product_inventory, product_guides
    return [{**r,'instructions':product_guides.GUIDES[r['id']]} for r in product_inventory.entries()]
