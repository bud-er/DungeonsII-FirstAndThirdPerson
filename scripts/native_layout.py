"""Measured per-build native addresses; unknown builds never inherit Steam RVAs."""

STEAM = 'steam-25647713'
GDK = 'gdk-1.1.1.0'


def profile(game):
    value = getattr(game, 'build_profile', STEAM)
    if value not in (STEAM, GDK):
        raise RuntimeError('Unknown native build profile')
    return value


def rva(game, steam_rva):
    if profile(game) == STEAM:
        return steam_rva
    from gdk_layout import ADDRESSES
    if steam_rva not in ADDRESSES:
        raise RuntimeError(f'No measured Xbox address for {steam_rva:X}')
    return ADDRESSES[steam_rva]


def matches(game, steam_rva, expected):
    address = rva(game, steam_rva)
    if profile(game) == GDK:
        from gdk_layout import GUARDS
        measured = GUARDS.get(steam_rva)
        if measured is None or measured[0][:len(expected)] != expected:
            raise RuntimeError('Xbox native guard is missing or its Steam reference changed')
        expected = measured[1][:len(expected)]
    return game.read(game.base + address, len(expected)) == expected
