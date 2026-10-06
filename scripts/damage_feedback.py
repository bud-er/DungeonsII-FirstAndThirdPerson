"""Local health-loss feedback. Hit direction awaits a verified event source.

Health loss alone does not identify an attacker. This deliberately displays an
undirected pulse instead of guessing a direction from nearby enemies.
"""
import math
import struct
import time

from damage_worker import DamageOverlayWorker as DamageOverlay, FADE_SECONDS


class HealthLoss:
    def __init__(self):
        self.previous = None

    def sample(self, health, maximum):
        if not all(math.isfinite(v) for v in (health, maximum)) or maximum <= 0 or not 0 <= health <= maximum*1.01:
            self.previous = None
            return 0.
        previous, self.previous = self.previous, (health, maximum)
        # A max-health/equipment change is not evidence of an incoming hit.
        if previous is None or abs(previous[1]-maximum) > .01:
            return 0.
        return max(0., previous[0]-health)


class DamageFeedback:
    def __init__(self, session):
        self.session, self.game = session, session.game
        self.loss = HealthLoss()
        self.overlay = None
        self.disabled = False
        self.hit_at = -math.inf
        self.next_tick = 0.
        session.ensure_layout('ATR_Health')
        health = [o['address'] for o in session.objects if o['outer'] == session.pawn
                  and not o['flags'] & 0x30 and session.is_a(o['class'], 'ATR_Health')]
        if len(health) != 1:
            raise RuntimeError('Expected one local health attribute set')
        self.health = health[0]
        session.remember(self.health, 'ATR_Health')
        layout = session.ensure_layout('GameplayAttributeData')
        current = next(p for p in layout['properties'] if p['name'] == 'CurrentValue')
        if current['type'] != 'FloatProperty' or current['size'] != 4:
            raise RuntimeError('Unexpected health attribute format')
        self.addresses = [session.field(self.health, 'ATR_Health', name)['address']+current['offset']
                          for name in ('Health', 'HealthMax')]

    def update(self, window):
        if self.disabled:
            return
        now = time.monotonic()
        if now < self.next_tick:
            return
        self.next_tick = now+1/60
        if self.session.identity(self.health) != self.session.identities[self.health]:
            self.close()
            self.disabled = True
            return
        health, maximum = (struct.unpack('<f', self.game.read(a, 4))[0] for a in self.addresses)
        lost = self.loss.sample(health, maximum)
        if lost > .01:
            self.hit_at = now
            print(f'Damage detected: -{lost:.2f} HP; displaying edge flash (direction unknown).', flush=True)
        intensity = max(0., 1-(now-self.hit_at)/FADE_SECONDS)
        try:
            # Start the display worker before the first hit. Native painting and
            # window creation never run in this camera/character update loop.
            if self.overlay is None:
                self.overlay = DamageOverlay(window)
            if self.overlay is not None:
                self.overlay.draw(intensity)
        except Exception as error:
            self.close()
            self.disabled = True
            print(f'Damage overlay unavailable: {error}', flush=True)

    def preview(self):
        self.hit_at = time.monotonic()
        print('Damage edge-flash preview; no health changed.', flush=True)

    def close(self):
        if self.overlay is not None:
            try:
                self.overlay.close()
            except Exception:
                pass
            self.overlay = None
