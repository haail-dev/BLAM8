"""Explicit booking-mode choices without reinterpreting booking history."""
from copy import deepcopy
from app import intake, services, setup_card_scalars, setup_rows

SPECS = {'stay_mode': dict(type='boolean', nullable=False, label='Booking type')}


def pending(card):
    return any(r.get('name') == 'setting' and r.get('fields', {}).get('key') == 'stay_mode'
               and r.get('status') in {'proposed', 'held'} for r in (card or {}).get('lines', []))


def parse(fields):
    return setup_card_scalars.parse(fields, SPECS)


def describe(fields):
    return 'Booking mode: ' + ('Arrival to departure' if fields['value'] else 'Appointments')


def prepare(line, snapshot):
    from app.setup_cards import Invalid
    try:
        fields = parse(line['fields'])
    except (ValueError, TypeError) as exc:
        raise Invalid(str(exc)) from None
    cfg = snapshot['config']
    before, after = intake.stay_mode(cfg), fields['value']
    if type(snapshot.get('has_bookings')) is not bool:
        raise Invalid('Booking history could not be verified.')
    if before != after and snapshot['has_bookings']:
        raise Invalid('Changing booking mode with saved bookings requires a migration. Existing bookings were not changed.')
    rows = deepcopy(cfg.get('intake_fields') or [])
    reserved = [r for r in rows if intake._is_stay_label(r.get('label'))]
    if len(reserved) > 1:
        raise Invalid('Resolve duplicate departure fields before choosing booking mode.')
    if after:
        for svc in (cfg.get('service_types') or []) if not before else []:
            pools = [r for r in cfg.get('resource_types') or [] if r.get('name') == svc.get('resource')]
            if services.timing(svc) != 'time' or svc.get('resource') != svc.get('name') or len(pools) != 1:
                raise Invalid('Stay-diary appointments need exact timing and their own same-name capacity pool first.')
            # An unpriced pool stores rate_unit 'booking' with rate None (legacy form + bind_appointment_pools);
            # the unit only means something with a rate, so validate it only when a rate is set.
            keys = setup_rows.KEYS['diary_settings', 'resource']
            pool = {k: v for k, v in pools[0].items() if k in keys and (k != 'rate_unit' or pools[0].get('rate') is not None)}
            if setup_rows.parse('diary_settings', 'resource', pool)['problems']:
                raise Invalid('The appointment capacity pool could not be verified.')
        if not before and cfg.get('service_types') and cfg.get('capacity_per_window') is None:
            from app import setup_card_stays
            if not setup_card_stays._stay_names(cfg):
                raise Invalid('Appointment pools cannot supply stay capacity. Choose separate stay capacity first.')
        if not reserved:
            rows.append(deepcopy(intake.STAY_FIELD))
    else:
        rows = [r for r in rows if not intake._is_stay_label(r.get('label'))]
    if len(rows) > intake.MAX_FIELDS:
        raise Invalid('There is no room for the departure field. No booking details were removed.')
    cfg_patch = {'booking_mode': 'stay' if after else 'appointment', 'intake_fields': rows}
    receipt = dict(id=line['id'], name='setting', fields=fields,
        before={'stay_mode': before, 'departure_field': bool(reserved)},
        after={'stay_mode': after, 'departure_field': after},
        notes=['Departure capture and the saved booking mode change together; other booking details are preserved.'])
    return cfg_patch, receipt
