"""Host-side model selection, matching pico_updater's assembly diagnostics."""

VARIANT_OPTIONS = {'自动识别': 'auto', '普通版': 'standard', 'W 版': 'wireless'}
MODELS = {'standard': 'LBJ 普通版', 'wireless': 'LBJ W 版'}
PINS = {'standard': {'power': 28, 'battery_adc': 27, 'temp_adc': 4, 'vbus': 24},
        'wireless': {'power': 42, 'battery_adc': 41, 'temp_adc': 8, 'vbus': 'WL_GPIO2'}}


def detect_variant(board):
    # Same rule as pico_updater.solder_check.is_wireless_board.
    return 'wireless' if 'RP2350' in str(board).upper() else 'standard'


def resolve_variant(board, requested='auto'):
    if requested not in ('auto', 'standard', 'wireless'):
        raise ValueError('未知检测型号：' + str(requested))
    identity = str(board).upper()
    if not any(chip in identity for chip in ('RP2040', 'RP2350')):
        raise ValueError('只支持本项目 RP2040 / RP2350 接线：' + str(board))
    variant = detect_variant(board) if requested == 'auto' else requested
    if variant == 'wireless' and 'RP2040' in identity:
        raise ValueError('W 版需要 GP41/42，RP2040 不支持；请选择普通版')
    return variant


def model_label(report):
    variant = report.get('hardware_variant')
    if variant not in MODELS:
        variant = detect_variant(report.get('board', ''))
    return MODELS[variant]


def serial_number(uid):
    n, value = int(uid, 16), ''
    while n:
        n, digit = divmod(n, 36)
        value = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[digit] + value
    return ('000000000000' + value)[-12:]
