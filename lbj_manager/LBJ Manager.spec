from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

source = Path(SPECPATH)
a = Analysis([str(source / 'lbj_manager.py')], pathex=[str(source)], binaries=[],
    datas=collect_data_files('customtkinter') + collect_data_files('tkintermapview') +
          [(str(source / 'diagnostic_drivers'), 'diagnostic_drivers')]
          + collect_data_files('tkinterdnd2', includes=['tkdnd/osx-arm64/*']),
    hiddenimports=collect_submodules('mpremote') + ['serial.tools.list_ports', 'solder_check'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['numpy', 'scipy', 'pandas', 'matplotlib', 'pytest', 'IPython'],
    noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LBJ Manager',
          console=False, strip=False, upx=False, argv_emulation=False,
          target_arch='arm64', codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LBJ Manager')
app = BUNDLE(coll, name='LBJ Manager.app', bundle_identifier='com.misakaxing.lbj.manager',
             version='3.0.4', info_plist={'NSHighResolutionCapable': True,
                                        'LSMinimumSystemVersion': '11.0'})
