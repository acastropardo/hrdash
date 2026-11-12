"""Exportacion de la sesion: FIT nativo Garmin (+ CSV espejo) con verificacion round-trip."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .history import Session
from .config import Config

MESG_FILE_ID = 0
MESG_SESSION = 18
MESG_LAP = 19
MESG_RECORD = 20
MESG_ACTIVITY = 34


def _stamp(session: Session) -> str:
    return datetime.fromtimestamp(session.start_ts).strftime("%Y-%m-%d_%H%M")


def export_csv(session: Session, config: Config, path: Path) -> Path:
    # La columna `bloque` solo aparece si hubo un plan: sin el, el CSV es el de siempre.
    plan = any(s.bloque for s in session.samples)
    lines = ["epoch,iso,heart_rate,zone_idx,zone,kcal_per_min,watts,cadence_rpm,speed_kmh,distance_m"
             + (",bloque" if plan else "")]
    for s in session.samples:
        zkey = f"Z{s.zone_idx}"
        lines.append(f"{s.epoch},{s.iso},{s.hr},{s.zone_idx},{zkey},{s.kcal_per_min:.4f},"
                     f"{'' if s.watts is None else s.watts},"
                     f"{'' if s.cadence is None else s.cadence},"
                     f"{'' if s.speed_kmh is None else s.speed_kmh},"
                     f"{'' if s.dist_m is None else s.dist_m}"
                     + (("," + (s.bloque or "").replace(",", " ")) if plan else ""))
    path.write_text("\n".join(lines) + "\n")
    return path


def export_fit(session: Session, config: Config, path: Path, sensor_name: str = "HRDash") -> Path:
    """Escribe un archivo FIT tipo actividad (records a 1 Hz + lap + session + activity)."""
    from garmin_fit_sdk import Encoder

    start = int(session.start_ts)
    end = int(session.samples[-1].epoch) if session.samples else start
    dur = max(1, end - start)
    kcal = int(round(session.total_kcal))
    utc_start = datetime.fromtimestamp(start, tz=timezone.utc)
    utc_end = datetime.fromtimestamp(end, tz=timezone.utc)

    enc = Encoder()

    enc.write_mesg({
        'mesg_num': MESG_FILE_ID,
        'type': 'activity',
        'manufacturer': 'garmin',
        'product': 0,
        'time_created': utc_start,
        'product_name': "HRDash",
    })

    last_ts = -1
    tr_data = (session._t["watts"][3] > 0 or session._t["speed_kmh"][3] > 0)
    for s in session.samples:
        if s.epoch <= last_ts:             # FIT exige timestamps estrictamente crecientes
            continue
        last_ts = s.epoch
        mesg = {
            'mesg_num': MESG_RECORD,
            'timestamp': datetime.fromtimestamp(s.epoch, tz=timezone.utc),
            'heart_rate': int(s.hr),
            'zone': int(s.zone_idx),
        }
        if tr_data:
            if s.watts:
                mesg['power'] = int(s.watts)
            if s.cadence:
                mesg['cadence'] = int(round(s.cadence))
            if s.speed_kmh:
                mesg['speed'] = s.speed_kmh / 3.6          # m/s (el encoder aplica scale)
            if s.dist_m is not None:
                mesg['distance'] = float(s.dist_m)         # metros
        enc.write_mesg(mesg)

    time_in_zone = [float(x) for x in session.zone_seconds]

    # eventos de sesion (start/stop)
    enc.write_mesg({'mesg_num': 21, 'timestamp': utc_start, 'event': 'session', 'event_type': 'start'})
    enc.write_mesg({'mesg_num': 21, 'timestamp': utc_end, 'event': 'session', 'event_type': 'stop'})

    enc.write_mesg({
        'mesg_num': MESG_LAP,
        'message_index': 0,
        'timestamp': utc_end,
        'sport': 'training',
        'event': 'lap',
        'event_type': 0,
        'start_time': utc_start,
        'total_elapsed_time': float(dur),
        'total_timer_time': float(dur),
        'total_calories': kcal,
        'avg_heart_rate': int(round(session.avg_hr)),
        'max_heart_rate': int(session.max_hr),
        'min_heart_rate': int(session.min_hr),
        'time_in_hr_zone': time_in_zone,
        **_trainer_fields(session, tr_data),
    })

    enc.write_mesg({
        'mesg_num': MESG_SESSION,
        'message_index': 0,
        'timestamp': utc_end,
        'sport': 'training',
        'start_time': utc_start,
        'total_elapsed_time': float(dur),
        'total_timer_time': float(dur),
        'num_laps': 1,
        'first_lap_index': 0,
        'total_calories': kcal,
        'avg_heart_rate': int(round(session.avg_hr)),
        'max_heart_rate': int(session.max_hr),
        'min_heart_rate': int(session.min_hr),
        'time_in_hr_zone': time_in_zone,
        **_trainer_fields(session, tr_data),
    })

    enc.write_mesg({
        'mesg_num': MESG_ACTIVITY,
        'total_timer_time': float(dur),
        'num_sessions': 1,
        'type': 'manual',
        'event': 'activity',
        'event_type': 1,
        'timestamp': utc_end,
    })

    data = enc.close()
    path.write_bytes(data)
    return path


def _trainer_fields(session: Session, tr_data: bool) -> dict:
    """Campos de potencia/velocidad/cadencia/distancia para lap y session."""
    if not tr_data:
        return {}
    f = {}
    if session._t["watts"][3] > 0:
        f['avg_power'] = int(round(session.avg_watts))
        f['max_power'] = int(session.max_watts)
    if session._t["speed_kmh"][3] > 0:
        f['avg_speed'] = round(session.avg_speed / 3.6, 3)   # m/s
        f['max_speed'] = round(session.max_speed / 3.6, 3)
        f['total_distance'] = float(session.total_dist_m)    # metros
    if session._t["cadence"][3] > 0:
        f['avg_cadence'] = int(round(session.avg_cadence))
        f['max_cadence'] = int(round(session.max_cadence))
    return f


def validate_fit(path: Path, session: Session) -> dict:
    """Decodifica el FIT recien escrito y comprueba consistencia (round-trip)."""
    from garmin_fit_sdk import Stream, Decoder

    stream = Stream.from_file(str(path))
    messages, errors = Decoder(stream).read(convert_datetimes_to_dates=False,
                                             convert_types_to_strings=False)
    recs = messages.get('record_mesgs', [])
    hrs = [r.get('heart_rate') for r in recs if isinstance(r.get('heart_rate'), (int, float))]
    pows = [r.get('power') for r in recs if isinstance(r.get('power'), (int, float))]
    summary = {
        'records': len(recs),
        'samples': session.n,
        'fit_avg_hr': round(sum(hrs) / len(hrs), 1) if hrs else None,
        'app_avg_hr': round(session.avg_hr, 1),
        'fit_max_hr': max(hrs) if hrs else None,
        'app_max_hr': session.max_hr,
        'fit_max_watts': max(pows) if pows else None,
        'app_max_watts': session.max_watts or None,
        'errors': len(errors),
        'bytes': path.stat().st_size,
    }
    ok = (summary['records'] == summary['samples']
          and summary['fit_max_hr'] == summary['app_max_hr']
          and (summary['fit_max_watts'] is None
               or summary['fit_max_watts'] == summary['app_max_watts']))
    summary['ok'] = ok
    return summary


def export_session(session: Session, config: Config, sensor_name: str,
                   out_dir: Path | None = None,
                   with_fit: bool | None = None,
                   with_csv: bool | None = None,
                   suffix: str = "") -> dict:
    """Graba la sesion. Devuelve {'fit': Path|None, 'csv': Path|None, 'validate': dict|None}.

    `suffix`: sufijo del nombre de archivo (p.ej. "_AUTO" o "_RESCATE").
    """
    do_fit = config.export_fit if with_fit is None else with_fit
    do_csv = config.export_csv if with_csv is None else with_csv
    out_dir = out_dir or config.effective_export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"hrdash_{_stamp(session)}{suffix}"

    result = {'fit': None, 'csv': None, 'validate': None}
    if session.n == 0:
        return result
    if do_csv:
        result['csv'] = export_csv(session, config, out_dir / f"{base}.csv")
    if do_fit:
        try:
            result['fit'] = export_fit(session, config, out_dir / f"{base}.fit", sensor_name)
            result['validate'] = validate_fit(result['fit'], session)
        except Exception as e:  # noqa: BLE001 - el CSV ya asegura el respaldo
            result['fit_error'] = str(e)
    return result
