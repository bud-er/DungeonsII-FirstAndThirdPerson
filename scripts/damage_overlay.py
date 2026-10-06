"""Soft per-pixel-alpha damage vignette, used only by the display worker."""
import ctypes as C
from ctypes import wintypes as W

WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [('style', W.UINT), ('procedure', WNDPROC), ('class_extra', C.c_int),
                ('window_extra', C.c_int), ('instance', W.HINSTANCE), ('icon', W.HICON),
                ('cursor', W.HANDLE), ('background', W.HBRUSH), ('menu', W.LPCWSTR), ('name', W.LPCWSTR)]


class BitmapInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('width', W.LONG), ('height', W.LONG),
                ('planes', W.WORD), ('bits', W.WORD), ('compression', W.DWORD),
                ('image_size', W.DWORD), ('x_pixels', W.LONG), ('y_pixels', W.LONG),
                ('colors_used', W.DWORD), ('colors_important', W.DWORD)]


class BlendFunction(C.Structure):
    _fields_ = [('operation', W.BYTE), ('flags', W.BYTE), ('alpha', W.BYTE), ('format', W.BYTE)]


def vignette_pixels(width, height):
    """Premultiplied BGRA; no black color-key bands or pixels in the center."""
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or width*height > 40_000_000:
        raise ValueError('Unexpected overlay dimensions')
    thickness = max(1, min(90, round(min(width, height)*.065)))
    # Build rows in slices instead of a full-screen Python pixel loop.
    ramp = [round(150*(1-i/thickness)**2) for i in range(thickness)]
    pixel = lambda alpha: bytes((round(30*alpha/255), round(20*alpha/255), round(235*alpha/255), alpha))
    rows = {}
    pixels = bytearray()
    for y in range(height):
        edge = min(y, height-1-y, thickness)
        if edge not in rows:
            vertical = ramp[edge] if edge < thickness else 0
            left = b''.join(pixel(max(vertical, ramp[x])) for x in range(min(thickness, width//2)))
            center_width = width-2*(len(left)//4)
            right = b''.join(left[i:i+4] for i in range(len(left)-4, -1, -4))
            rows[edge] = left+pixel(vertical)*center_width+right
        pixels.extend(rows[edge])
    return bytes(pixels)


class DamageOverlay:
    def __init__(self, window):
        self.window, self.hwnd = window, None
        self.width, self.height = 0, 0
        self.visible, self.last_alpha = False, None
        self.dc = self.bitmap = self.old_bitmap = None
        self.user = C.WinDLL('user32', use_last_error=True)
        self.gdi = C.WinDLL('gdi32', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.user.SetProcessDPIAware()
        self.kernel.GetModuleHandleW.argtypes, self.kernel.GetModuleHandleW.restype = [W.LPCWSTR], W.HMODULE
        signatures = {
            'RegisterClassW': ([C.POINTER(WindowClass)], W.ATOM),
            'UnregisterClassW': ([W.LPCWSTR, W.HINSTANCE], W.BOOL),
            'CreateWindowExW': ([W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int,
                                 C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p], W.HWND),
            'DefWindowProcW': ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], C.c_ssize_t),
            'DestroyWindow': ([W.HWND], W.BOOL),
            'ShowWindow': ([W.HWND, C.c_int], W.BOOL),
            'GetForegroundWindow': ([], W.HWND),
            'IsIconic': ([W.HWND], W.BOOL),
            'IsWindow': ([W.HWND], W.BOOL),
            'GetClientRect': ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            'ClientToScreen': ([W.HWND, C.POINTER(W.POINT)], W.BOOL),
            'SetWindowPos': ([W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL),
            'UpdateLayeredWindow': ([W.HWND, W.HDC, C.POINTER(W.POINT), C.POINTER(W.SIZE), W.HDC,
                                     C.POINTER(W.POINT), W.DWORD, C.POINTER(BlendFunction), W.DWORD], W.BOOL),
            'ValidateRect': ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            'PeekMessageW': ([C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT], W.BOOL),
            'DispatchMessageW': ([C.POINTER(W.MSG)], C.c_ssize_t),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ('CreateCompatibleDC', [W.HDC], W.HDC),
            ('CreateDIBSection', [W.HDC, C.POINTER(BitmapInfo), W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP),
            ('SelectObject', [W.HDC, W.HGDIOBJ], W.HGDIOBJ),
            ('DeleteObject', [W.HGDIOBJ], W.BOOL),
            ('DeleteDC', [W.HDC], W.BOOL),
        ):
            fn = getattr(self.gdi, name)
            fn.argtypes, fn.restype = args, result
        self.instance = self.kernel.GetModuleHandleW(None)
        self.name = f'DungeonsFirstPersonDamage_{id(self):x}'
        self.callback = WNDPROC(self.message)
        cls = WindowClass(procedure=self.callback, instance=self.instance, name=self.name)
        if not self.user.RegisterClassW(C.byref(cls)):
            raise C.WinError(C.get_last_error())
        try:
            # NOACTIVATE | TRANSPARENT | TOOLWINDOW | LAYERED | TOPMOST.
            self.hwnd = self.user.CreateWindowExW(0x080800A8, self.name, 'Dungeons damage feedback',
                                                 0x80000000, 0, 0, 1, 1, window, None, self.instance, None)
            if not self.hwnd:
                raise C.WinError(C.get_last_error())
            self.prepare()  # Cache the bitmap before any hit; keep the window hidden.
        except Exception:
            self.close()
            raise

    def message(self, hwnd, message, wparam, lparam):
        if message == 0x84:  # WM_NCHITTEST
            return -1
        if message == 0x21:  # WM_MOUSEACTIVATE
            return 3
        if message == 0x14:  # WM_ERASEBKGND
            return 1
        if message == 0x0F:  # Pixel content comes only from UpdateLayeredWindow.
            self.user.ValidateRect(hwnd, None)
            return 0
        return self.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def release_bitmap(self):
        if self.dc:
            if self.old_bitmap:
                self.gdi.SelectObject(self.dc, self.old_bitmap)
            if self.bitmap:
                self.gdi.DeleteObject(self.bitmap)
            self.gdi.DeleteDC(self.dc)
        self.dc = self.bitmap = self.old_bitmap = None

    def prepare(self):
        rect, origin = W.RECT(), W.POINT()
        if not self.user.GetClientRect(self.window, C.byref(rect)) or not self.user.ClientToScreen(self.window, C.byref(origin)):
            raise C.WinError(C.get_last_error())
        width, height = rect.right-rect.left, rect.bottom-rect.top
        if width <= 0 or height <= 0:
            return None
        if (width, height) != (self.width, self.height):
            pixels = vignette_pixels(width, height)
            self.release_bitmap()
            self.dc = self.gdi.CreateCompatibleDC(None)
            if not self.dc:
                raise C.WinError(C.get_last_error())
            info = BitmapInfo(size=C.sizeof(BitmapInfo), width=width, height=-height, planes=1, bits=32)
            bits = C.c_void_p()
            self.bitmap = self.gdi.CreateDIBSection(self.dc, C.byref(info), 0, C.byref(bits), None, 0)
            if not self.bitmap or not bits.value:
                raise C.WinError(C.get_last_error())
            self.old_bitmap = self.gdi.SelectObject(self.dc, self.bitmap)
            if not self.old_bitmap or self.old_bitmap == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            C.memmove(bits, pixels, len(pixels))
            self.width, self.height = width, height
            self.last_alpha = None
        return origin

    def draw(self, intensity):
        msg = W.MSG()
        while self.user.PeekMessageW(C.byref(msg), self.hwnd, 0, 0, 1):
            self.user.DispatchMessageW(C.byref(msg))
        if not self.user.IsWindow(self.window):
            raise RuntimeError('Game window closed')
        if intensity <= 0 or self.user.IsIconic(self.window) or self.user.GetForegroundWindow() != self.window:
            if self.visible:
                self.user.ShowWindow(self.hwnd, 0)
                self.visible = False
            return
        origin = self.prepare()
        if origin is None:
            return
        alpha = round(255*max(0., min(1., intensity)))
        position = (origin.x, origin.y)
        if alpha != self.last_alpha or position != getattr(self, 'last_position', None):
            blend, size, source = BlendFunction(0, 0, alpha, 1), W.SIZE(self.width, self.height), W.POINT()
            if not self.user.UpdateLayeredWindow(self.hwnd, None, C.byref(origin), C.byref(size), self.dc,
                                                 C.byref(source), 0, C.byref(blend), 2):
                raise C.WinError(C.get_last_error())
            self.last_alpha, self.last_position = alpha, position
        if not self.visible:
            # Only show once per pulse; fading never repositions/repaints the game.
            if not self.user.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x10 | 0x40 | 0x01 | 0x02):
                raise C.WinError(C.get_last_error())
            self.visible = True

    def close(self):
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.release_bitmap()
        self.user.UnregisterClassW(self.name, self.instance)
