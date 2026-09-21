# -*- coding: utf-8 -*-
"""
诊断脚本 03：完整 _attach() 序列 dry-run
- 不启动 NSSM，纯模拟 main() 的 4.5~4.8 步骤（_attach 调用）
- 捕获任何 TypeError / NameError / AttributeError
- 这是 v2.0.2 启动失败的核心假设
"""
import sys, os, traceback
sys.path.insert(0, '.')

print('=' * 60)
print('STEP 0: import 联网_service')
print('=' * 60)
try:
    import 联网_service as main
    print('  OK')
except Exception as exc:
    print(f'  FAIL: {type(exc).__name__}: {exc}')
    traceback.print_exc()
    sys.exit(1)

print()
print('=' * 60)
print('STEP 1: 模拟 main() 4.5 ~ 4.8 _attach 调用序列')
print('=' * 60)

# 模拟 main() 中的 _attach 调用（直接读源码提取的 kwargs）
attach_calls = []

# 4.5 _protocol_mod._attach(...)
try:
    main._protocol_mod._attach(
        logger=main.logger,
        state=main.STATE,
        state_lock=main.STATE_LOCK,
        backoff=main.BACKOFF,
        backoff_lock=main.BACKOFF_LOCK,
        run_lock=main.RUN_LOCK,
        pwd_value=main._PWD_VALUE,
        pwd_lock=main.PWD_LOCK,
        load_password_from_disk=main._load_password_from_disk,
        get_password=main._get_password,
        load_config=main._load_config,
        set_state=main._set_state,
        set_backoff=main._set_backoff,
        reset_backoff=main._reset_backoff,
        backoff_until=main._backoff_until,
        now_iso=main._now_iso,
        stop_event=main.STOP_EVENT,
    )
    print(f'  [4.5] _protocol_mod._attach(...)     OK')
except Exception as exc:
    print(f'  [4.5] _protocol_mod._attach(...)     FAIL: {type(exc).__name__}: {exc}')
    traceback.print_exc()

# 4.6 _eula_mod._attach(base_dir=BASE_DIR)
try:
    main._eula_mod._attach(base_dir=main.BASE_DIR)
    print(f'  [4.6] _eula_mod._attach(base_dir)   OK')
except Exception as exc:
    print(f'  [4.6] _eula_mod._attach(base_dir)   FAIL: {type(exc).__name__}: {exc}')
    traceback.print_exc()

# 4.7 _web_api_mod._attach(...)
try:
    main._web_api_mod._attach(
        logger=main.logger,
        run_lock=main.RUN_LOCK,
        base_dir=main.BASE_DIR,
        log_file=main.LOG_FILE,
        log_dir=main.LOG_DIR,
        state=main.STATE,
        state_lock=main.STATE_LOCK,
        pwd_value=main._PWD_VALUE,
        pwd_lock=main.PWD_LOCK,
        config_file=main.CONFIG_FILE,
        password_file=main.PASSWORD_FILE,
        upgrade_log_file=main.UPGRADE_LOG_FILE,
        default_config=main.DEFAULT_CONFIG,
        allowed_suffixes=main.ALLOWED_SUFFIXES,
        allowed_intervals=main.ALLOWED_INTERVALS,
        allowed_update_intervals=main.ALLOWED_UPDATE_INTERVALS,
        load_config=main._load_config,
        save_config=main._save_config,
        save_password_to_disk=main._save_password_to_disk,
        validate_config=main._validate_config,
        snapshot_state=main._snapshot_state,
        get_password=main._get_password,
        now_iso=main._now_iso,
        stop_event=main.STOP_EVENT,
        run_once_fn=main.run_once,
    )
    print(f'  [4.7] _web_api_mod._attach(...)      OK')
except Exception as exc:
    print(f'  [4.7] _web_api_mod._attach(...)      FAIL: {type(exc).__name__}: {exc}')
    traceback.print_exc()

# 4.8 _auto_update_mod._attach(...)
try:
    main._auto_update_mod._attach(
        logger=main.logger,
        state=main.STATE,
        state_lock=main.STATE_LOCK,
        update_lock=main.UPDATE_LOCK,
        tools_dir=main.TOOLS_DIR,
        nssm_path=main.NSSM_PATH,
        upgrade_log_file=main.UPGRADE_LOG_FILE,
        log_dir=main.LOG_DIR,
        base_dir=main.BASE_DIR,
        load_config=main._load_config,
        save_config=main._save_config,
        now_iso=main._now_iso,
        stop_event=main.STOP_EVENT,
    )
    print(f'  [4.8] _auto_update_mod._attach(...)  OK')
except Exception as exc:
    print(f'  [4.8] _auto_update_mod._attach(...)  FAIL: {type(exc).__name__}: {exc}')
    traceback.print_exc()

print()
print('=' * 60)
print('STEP 2: 验证 web_api 模块被 _attach 后 globals 正确')
print('=' * 60)
import web_api
critical_after = ['STATE', 'STATE_LOCK', 'BACKOFF', 'RUN_LOCK', '_PWD_VALUE', 'PWD_LOCK',
                   'BASE_DIR', 'CONFIG_FILE', 'PASSWORD_FILE', 'LOG_FILE', 'LOG_DIR',
                   'UPGRADE_LOG_FILE', 'DEFAULT_CONFIG', 'ALLOWED_SUFFIXES',
                   'ALLOWED_INTERVALS', 'ALLOWED_UPDATE_INTERVALS',
                   '_load_config', '_save_config', '_save_password_to_disk',
                   '_validate_config', '_snapshot_state', '_get_password',
                   '_now_iso', 'STOP_EVENT', 'run_once', 'logger',
                   'api_get_changelog', '_auto_update_mod']
missing_after = []
for n in critical_after:
    val = getattr(web_api, n, 'MISSING')
    if val == 'MISSING':
        missing_after.append(n)
print(f'  web_api globals after _attach: {len(critical_after) - len(missing_after)}/{len(critical_after)} present')
if missing_after:
    print(f'  MISSING: {missing_after}')

print()
print('=' * 60)
print('STEP 3: 验证 _auto_update_mod 在 web_api 中是 None 还是模块')
print('=' * 60)
import auto_update as au_mod
print(f'  web_api._auto_update_mod is None: {web_api._auto_update_mod is None}')
print(f'  web_api._auto_update_mod is auto_update module: {web_api._auto_update_mod is au_mod}')
