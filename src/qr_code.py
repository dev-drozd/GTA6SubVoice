from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

# Таблица параметров QR-кода для уровня коррекции M (версии 1..10):
# (всего_байт_данных, ec_байт_на_блок, кол-во_блоков_g1, данных_в_g1, кол-во_блоков_g2, данных_в_g2)
_QR_VERSION_M: dict[int, tuple[int, int, int, int, int, int]] = {
    1: (16, 10, 1, 16, 0, 0),
    2: (28, 16, 1, 28, 0, 0),
    3: (44, 26, 1, 44, 0, 0),
    4: (64, 18, 2, 32, 0, 0),
    5: (86, 24, 2, 43, 0, 0),
    6: (108, 16, 4, 27, 0, 0),
    7: (124, 18, 4, 31, 0, 0),
    8: (154, 22, 2, 38, 2, 39),
    9: (182, 22, 3, 36, 2, 37),
    10: (216, 26, 4, 43, 1, 44),
}

# Координаты центров выравнивающих узоров (Alignment Patterns) по версиям 1..10
_ALIGNMENT_POSITIONS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

# Остаточные биты (remainder bits) после размещения всех кодовых слов
_REMAINDER_BITS: dict[int, int] = {
    1: 0,
    2: 7,
    3: 7,
    4: 7,
    5: 7,
    6: 7,
    7: 0,
    8: 0,
    9: 0,
    10: 0,
}


def _init_gf256() -> tuple[list[int], list[int]]:
    """Строит таблицы экспоненты и логарифма для поля Галуа GF(256) с полиномом 0x11d."""
    exp = [0] * 512
    log = [0] * 256
    val = 1
    for i in range(255):
        exp[i] = val
        log[val] = i
        val <<= 1
        if val & 0x100:
            val ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _init_gf256()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    """Генерирует полином Рида-Соломона заданной степени."""
    poly = [1]
    for i in range(degree):
        # Умножаем poly на (x - alpha^i) == (x + alpha^i) в GF(256)
        factor = [1, _GF_EXP[i]]
        nxt = [0] * (len(poly) + 1)
        for pi, pv in enumerate(poly):
            for fi, fv in enumerate(factor):
                nxt[pi + fi] ^= _gf_mul(pv, fv)
        poly = nxt
    return poly


def _rs_compute_ec(data: list[int], ec_len: int) -> list[int]:
    """Вычисляет байты коррекции ошибок Рида-Соломона."""
    gen = _rs_generator_poly(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data) :]


def _choose_version(payload_len: int) -> int:
    """Подбирает минимальную версию QR-кода (1..10) для Byte mode уровня M."""
    for ver, (total_data, _ec, _g1, _d1, _g2, _d2) in _QR_VERSION_M.items():
        # 4 бита индикатор режима + 8/16 бит длина + данные + 4 бита терминатора
        count_bits = 8 if ver < 10 else 16
        capacity_bytes = (total_data * 8 - 4 - count_bits) // 8
        if payload_len <= capacity_bytes:
            return ver
    raise ValueError(f"Строка слишком длинная для QR-кода v1..v10 ({payload_len} байт)")


def _encode_codewords(payload: bytes, ver: int) -> list[int]:
    """Кодирует данные, добавляет выравнивание, разбивает на блоки и дописывает ECC."""
    total_data, ec_len, g1_blocks, g1_len, g2_blocks, g2_len = _QR_VERSION_M[ver]
    bits: list[int] = []

    def _push_bits(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    # Индикатор режима Byte (0100) + счётчик длины
    _push_bits(0b0100, 4)
    _push_bits(len(payload), 8 if ver < 10 else 16)
    for b in payload:
        _push_bits(b, 8)

    max_bits = total_data * 8
    terminator = min(4, max_bits - len(bits))
    bits.extend([0] * terminator)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_bytes = [
        sum(bits[i + b] << (7 - b) for b in range(8))
        for i in range(0, len(bits), 8)
    ]
    pad_cycle = (0xEC, 0x11)
    pad_idx = 0
    while len(data_bytes) < total_data:
        data_bytes.append(pad_cycle[pad_idx % 2])
        pad_idx += 1

    # Разбиение на блоки и расчёт ECC
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for count, blk_len in ((g1_blocks, g1_len), (g2_blocks, g2_len)):
        for _ in range(count):
            blk = data_bytes[offset : offset + blk_len]
            offset += blk_len
            data_blocks.append(blk)
            ec_blocks.append(_rs_compute_ec(blk, ec_len))

    # Чередование (interleaving) данных и байтов ECC
    out: list[int] = []
    max_data_blk = max(len(b) for b in data_blocks)
    for col in range(max_data_blk):
        for blk in data_blocks:
            if col < len(blk):
                out.append(blk[col])
    for col in range(ec_len):
        for eblk in ec_blocks:
            out.append(eblk[col])
    return out


def _place_finder(grid: list[list[Optional[bool]]], reserved: list[list[bool]], r0: int, c0: int) -> None:
    size = len(grid)
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r0 + dr, c0 + dc
            if not (0 <= rr < size and 0 <= cc < size):
                continue
            reserved[rr][cc] = True
            if dr in (-1, 7) or dc in (-1, 7):
                grid[rr][cc] = False
            elif dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4):
                grid[rr][cc] = True
            else:
                grid[rr][cc] = False


def _place_alignment(grid: list[list[Optional[bool]]], reserved: list[list[bool]], cr: int, cc: int) -> None:
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            rr, cc2 = cr + dr, cc + dc
            reserved[rr][cc2] = True
            if abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0):
                grid[rr][cc2] = True
            else:
                grid[rr][cc2] = False


def _format_bits(mask_id: int) -> int:
    """Вычисляет 15-битную строку формата для уровня M (00) и заданной маски 0..7."""
    # Уровень коррекции M = 0b00
    data = (0b00 << 3) | (mask_id & 0b111)
    rem = data << 10
    gen = 0b10100110111
    for bit in range(14, 9, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 10)
    raw = (data << 10) | rem
    return raw ^ 0b101010000010010


def _version_bits(ver: int) -> int:
    """Вычисляет 18-битную строку версии для версий >= 7."""
    rem = ver << 12
    gen = 0b1111100100101
    for bit in range(17, 11, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 12)
    return (ver << 12) | rem


def _mask_condition(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0:
        return (r + c) % 2 == 0
    if mask_id == 1:
        return r % 2 == 0
    if mask_id == 2:
        return c % 3 == 0
    if mask_id == 3:
        return (r + c) % 3 == 0
    if mask_id == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask_id == 5:
        return ((r * c) % 2 + (r * c) % 3) == 0
    if mask_id == 6:
        return (((r * c) % 2 + (r * c) % 3) % 2) == 0
    return (((r + c) % 2 + (r * c) % 3) % 2) == 0


def _penalty_score(mat: list[list[bool]]) -> int:
    """Оценка качества маски QR по стандартным 4 правилам штрафа ISO/IEC 18004."""
    n = len(mat)
    score = 0

    # Правило 1: 5+ одинаковых модулей подряд по строкам и столбцам
    for r in range(n):
        run = 1
        for c in range(1, n):
            if mat[r][c] == mat[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(n):
        run = 1
        for r in range(1, n):
            if mat[r][c] == mat[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # Правило 2: блоки 2x2 одного цвета
    for r in range(n - 1):
        for c in range(n - 1):
            v = mat[r][c]
            if mat[r][c + 1] == v and mat[r + 1][c] == v and mat[r + 1][c + 1] == v:
                score += 3

    # Правило 3: ложные узоры finder (1:1:3:1:1)
    pat_a = (True, False, True, True, True, False, True, False, False, False, False)
    pat_b = (False, False, False, False, True, False, True, True, True, False, True)
    for r in range(n):
        row = mat[r]
        for c in range(n - 10):
            seg = tuple(row[c : c + 11])
            if seg == pat_a or seg == pat_b:
                score += 40
    for c in range(n):
        for r in range(n - 10):
            seg = tuple(mat[r + k][c] for k in range(11))
            if seg == pat_a or seg == pat_b:
                score += 40

    # Правило 4: баланс тёмных/светлых модулей около 50%
    dark = sum(1 for row in mat for v in row if v)
    pct = (dark * 100) // (n * n)
    prev_5 = abs(pct - (pct % 5) - 50) // 5
    next_5 = abs(pct + (5 - (pct % 5)) - 50) // 5
    score += min(prev_5, next_5) * 10
    return score


def build_qr_matrix(text: str) -> list[list[bool]]:
    """Строит матрицу QR-кода (True = тёмный модуль) для произвольной UTF-8 строки."""
    payload = text.encode("utf-8")
    ver = _choose_version(len(payload))
    size = 17 + 4 * ver

    grid: list[list[Optional[bool]]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    # Позиционные узоры (Finder patterns)
    _place_finder(grid, reserved, 0, 0)
    _place_finder(grid, reserved, 0, size - 7)
    _place_finder(grid, reserved, size - 7, 0)

    # Выравнивающие узоры (Alignment patterns) ставим до линий синхронизации,
    # избегая только областей вокруг трёх позиционных узоров
    apos = _ALIGNMENT_POSITIONS[ver]
    for r in apos:
        for c in apos:
            if not reserved[r][c]:
                _place_alignment(grid, reserved, r, c)

    # Синхронизирующие линии (Timing patterns)
    for i in range(8, size - 8):
        bit = (i % 2) == 0
        reserved[6][i] = True
        grid[6][i] = bit
        reserved[i][6] = True
        grid[i][6] = bit

    # Тёмный модуль (Dark module)
    reserved[4 * ver + 9][8] = True
    grid[4 * ver + 9][8] = True

    # Резервируем зоны форматной и версионной информации
    for i in range(9):
        if i != 6:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    if ver >= 7:
        vbits = _version_bits(ver)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            r = i // 3
            c = size - 11 + (i % 3)
            reserved[r][c] = True
            grid[r][c] = bit
            reserved[c][r] = True
            grid[c][r] = bit

    # Преобразуем поток кодовых слов в битовый список
    codewords = _encode_codewords(payload, ver)
    bit_stream: list[bool] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bit_stream.append(bool((cw >> shift) & 1))
    bit_stream.extend([False] * _REMAINDER_BITS[ver])

    # Размещение битов зигзагом справа налево
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                cc = col + dc
                if not reserved[r][cc]:
                    val = bit_stream[bit_idx] if bit_idx < len(bit_stream) else False
                    grid[r][cc] = val
                    bit_idx += 1
        col -= 2
        upward = not upward

    # Перебор 8 масок и выбор оптимальной по минимальному штрафу
    best_mat: Optional[list[list[bool]]] = None
    best_score = 10**9
    for mask_id in range(8):
        cand: list[list[bool]] = [[False] * size for _ in range(size)]
        for r in range(size):
            for c in range(size):
                base_val = bool(grid[r][c])
                if not reserved[r][c] and _mask_condition(mask_id, r, c):
                    cand[r][c] = not base_val
                else:
                    cand[r][c] = base_val

        # Записываем 15 бит формата (от старшего b14 к младшему b0 по ISO/IEC 18004)
        fbits = _format_bits(mask_id)
        for i in range(15):
            bit = bool((fbits >> (14 - i)) & 1)
            # Первая копия: вокруг левого верхнего угла
            if i < 6:
                cand[8][i] = bit
            elif i == 6:
                cand[8][7] = bit
            elif i == 7:
                cand[8][8] = bit
            elif i == 8:
                cand[7][8] = bit
            else:
                cand[14 - i][8] = bit

            # Вторая копия: снизу слева (7 бит: b14..b8) и справа сверху (8 бит: b7..b0)
            if i < 7:
                cand[size - 1 - i][8] = bit
            else:
                cand[8][size - 15 + i] = bit

        sc = _penalty_score(cand)
        if sc < best_score:
            best_score = sc
            best_mat = cand

    assert best_mat is not None
    return best_mat


def render_qr_qimage(text: str, target_px: int = 168, quiet_modules: int = 3) -> QImage:
    """Рендерит QR-код в чёткий монохромный QImage (без размытия, с белой рамкой)."""
    mat = build_qr_matrix(text)
    n = len(mat)
    total_modules = n + quiet_modules * 2
    scale = max(2, target_px // total_modules)
    side = total_modules * scale

    img = QImage(side, side, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)

    black = 0xFF0B0A16
    for r in range(n):
        for c in range(n):
            if mat[r][c]:
                y0 = (r + quiet_modules) * scale
                x0 = (c + quiet_modules) * scale
                for dy in range(scale):
                    for dx in range(scale):
                        img.setPixel(x0 + dx, y0 + dy, black)

    if side != target_px:
        return img.scaled(
            target_px,
            target_px,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    return img


def render_qr_pixmap(text: str, target_px: int = 168, quiet_modules: int = 3) -> QPixmap:
    """Возвращает QPixmap с QR-кодом для отображения в Qt-интерфейсе."""
    return QPixmap.fromImage(render_qr_qimage(text, target_px=target_px, quiet_modules=quiet_modules))
