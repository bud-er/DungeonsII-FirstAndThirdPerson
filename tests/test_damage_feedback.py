from pathlib import Path
import sys
import unittest
import struct
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from damage_feedback import HealthLoss, DamageFeedback
from damage_overlay import vignette_pixels


class HealthLossDetection(unittest.TestCase):
    def test_initial_health_healing_and_unchanged_health_are_not_hits(self):
        loss = HealthLoss()
        self.assertEqual([loss.sample(v, 100) for v in (80, 80, 100, 100)], [0, 0, 0, 0])

    def test_real_health_loss_including_lethal_hit(self):
        loss = HealthLoss()
        loss.sample(100, 100)
        self.assertEqual(loss.sample(93.5, 100), 6.5)
        self.assertEqual(loss.sample(0, 100), 93.5)

    def test_equipment_max_health_change_is_not_damage(self):
        loss = HealthLoss()
        loss.sample(100, 100)
        self.assertEqual(loss.sample(75, 75), 0)
        self.assertEqual(loss.sample(70, 75), 5)

    def test_invalid_samples_reset_baseline(self):
        loss = HealthLoss()
        loss.sample(100, 100)
        self.assertEqual(loss.sample(float('nan'), 100), 0)
        self.assertEqual(loss.sample(70, 100), 0)

    def test_real_health_drop_reaches_overlay_without_writing_game_memory(self):
        feedback = DamageFeedback.__new__(DamageFeedback)
        feedback.disabled, feedback.next_tick, feedback.hit_at = False, 0, -float('inf')
        feedback.health, feedback.addresses = 10, [100, 200]
        feedback.overlay, feedback.loss = None, HealthLoss()
        feedback.session = SimpleNamespace(identity=Mock(return_value='same'), identities={10: 'same'})
        feedback.game = Mock()
        feedback.game.read.side_effect = [struct.pack('<f', n) for n in (100, 100, 94, 100)]
        with patch('damage_feedback.time.monotonic', side_effect=[1, 2]), patch('damage_feedback.DamageOverlay') as overlay:
            feedback.update(123)
            overlay.assert_called_once_with(123)  # Warm the worker before the first hit.
            overlay.return_value.draw.assert_called_once_with(0.)
            overlay.return_value.draw.reset_mock()
            feedback.update(123)
            overlay.assert_called_once_with(123)
            overlay.return_value.draw.assert_called_once_with(1.)
        feedback.game.write.assert_not_called()

    def test_vignette_is_transparent_in_center_and_has_premultiplied_soft_edges(self):
        for width, height in ((1280, 720), (2560, 1440), (3840, 2160)):
            pixels = vignette_pixels(width, height)
            self.assertEqual(len(pixels), width*height*4)
            def pixel(x, y):
                offset = (y*width+x)*4
                return pixels[offset:offset+4]
            self.assertEqual(pixel(width//2, height//2), bytes(4))
            self.assertTrue(0 < pixel(0, height//2)[3] < 200)
            self.assertGreater(pixel(0, height//2)[3], pixel(10, height//2)[3])
            self.assertEqual(pixel(0, height//2), pixel(width-1, height//2))
            for y in (0, 10, height//2):
                for x in (0, 10, width//2):
                    b, g, r, alpha = pixel(x, y)
                    self.assertTrue(max(b, g, r) <= alpha)


if __name__ == '__main__':
    unittest.main()
