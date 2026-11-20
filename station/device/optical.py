import sqlite3

from station.device.fixture import OpticalObservation
from station.device.recording import DeviceRecording, ExposureRecord
from station.device.terminal import ConfirmedMissing, RawReading, Terminal
from station.rounds.repository import RoundRepository, transaction

__all__ = ['apply_observation', 'observe_dual']


def observe_dual(connection: sqlite3.Connection, exposure: ExposureRecord,
                 observation: OpticalObservation) -> tuple[dict, ...]:
    """Consume one dual-head observation and return the messages it publishes."""
    from station.device.dual import observe_dual as _observe_dual
    return _observe_dual(connection, exposure, observation)


def apply_observation(connection: sqlite3.Connection, exposure: ExposureRecord,
                      observation: OpticalObservation) -> Terminal | None:
    if observation.head != 'left':
        raise ValueError('the station exposure uses the left head')
    with transaction(connection, write=True):
        recordings = DeviceRecording(connection)
        if recordings.find_exposure(exposure.order_id) != exposure:
            raise ValueError('observation targets an unknown or conflicting exposure')
        round_record = RoundRepository(connection).require_round(exposure.round_id)
        if round_record.mode != 'single':
            raise ValueError(f'round {exposure.round_id!r} does not use the single-head source')
        if observation.kind == 'empty':
            return None
        binding = (f'original:{exposure.trigger_id}', exposure.round_id, exposure.piece,
                   exposure.order_id, exposure.trigger_id)
        if observation.kind == 'reading':
            terminal = RawReading(*binding, observation.tick, observation.raw_code)
        else:
            terminal = ConfirmedMissing(*binding)
        return recordings.finalize(terminal)
