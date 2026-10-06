"""Keep all overlay/window work outside the camera and character update process."""
import multiprocessing as mp
import time

FADE_SECONDS = .5


def render_loop(window, deadline, stop, errors, renderer_factory=None):
    # This process has no game-memory reader, writer, or input-control object.
    overlay = None
    try:
        if renderer_factory is None:
            from damage_overlay import DamageOverlay
            renderer_factory = DamageOverlay
        overlay = renderer_factory(window)
        parent = mp.parent_process()
        while not stop.is_set() and (parent is None or parent.is_alive()):
            remaining = deadline.value-time.monotonic()
            overlay.draw(max(0., min(1., remaining/FADE_SECONDS)))
            stop.wait(1/30)
    except Exception as error:
        try:
            errors.send(f'{type(error).__name__}: {error}'[:1000])
        except (BrokenPipeError, OSError):
            pass
    finally:
        if overlay is not None:
            overlay.close()
        errors.close()


class DamageOverlayWorker:
    def __init__(self, window, *, renderer_factory=None):
        context = mp.get_context('spawn')
        self.deadline = context.RawValue('d', 0.)
        self.stop = context.Event()
        self.errors, sender = context.Pipe(duplex=False)
        self.process = context.Process(target=render_loop,
                                       args=(window, self.deadline, self.stop, sender, renderer_factory),
                                       name='Dungeons damage display', daemon=True)
        self.closed = False
        self.process.start()
        sender.close()

    def draw(self, intensity):
        if self.closed:
            return
        if self.errors.poll():
            try:
                message = self.errors.recv()
            except EOFError:
                message = 'Damage display process exited'
            raise RuntimeError(message)
        if not self.process.is_alive():
            raise RuntimeError('Damage display process exited')
        # One latest-value slot: slow painting cannot block or queue camera ticks.
        self.deadline.value = time.monotonic()+FADE_SECONDS*max(0., min(1., intensity)) if intensity > 0 else 0.

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.deadline.value = 0.
        self.stop.set()
        self.process.join(.15)
        if self.process.is_alive():
            # This is our display-only child, never the game or camera helper.
            self.process.terminate()
            self.process.join(.15)
        self.errors.close()
        if not self.process.is_alive():
            self.process.close()
