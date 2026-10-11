// A track's opening lift is held for one second and reaches unity at eight,
// falling linearly in decibels: the same curve as intro_lift.py applies on
// the server.  The lift is the server's peak-safe value, capped at the 6 dB
// the setting allows.
const MAX_LIFT_DB = 6;

export function introMultiplier(seconds, liftDb) {
  const weight = Math.max(0, Math.min(1, (8 - seconds) / 7));
  const lift = Number.isFinite(liftDb) ? Math.max(0, Math.min(MAX_LIFT_DB, liftDb)) : 0;
  return 10 ** (lift * weight / 20);
}

export function scheduleIntroGain(param, now, position, liftDb) {
  param.cancelScheduledValues(now);
  param.setValueAtTime(introMultiplier(position, liftDb), now);
  if (position < 1) param.setValueAtTime(introMultiplier(1, liftDb), now + 1 - position);
  if (position < 8) {
    const end = now + 8 - position;
    if (param.exponentialRampToValueAtTime) param.exponentialRampToValueAtTime(1, end);
    else param.linearRampToValueAtTime(1, end);
  }
}
