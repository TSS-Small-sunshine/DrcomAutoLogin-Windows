# -*- coding: utf-8 -*-
"""
诊断脚本 05：精确模拟装包环境
- 复制 联网_service.py 到临时目录（不带解耦模块）
- cd 到临时目录，模拟 NSSM 启动
- 验证 import 失败 → ModuleNotFoundError
"""
import sys, os, shutil, tempfile, traceback, subprocess

# 复制 联网_service.py 到临时目录，模拟"装包后只装了主入口 .py"
tmpdir = tempfile.mkdtemp(prefix='drcom_sim_')
print(f'[sim] temp dir = {tmpdir}')

# 只复制联网_service.py（模拟 setup.iss 的 [Files] 段行为）
shutil.copy('联网_service.py', os.path.join(tmpdir, '联网_service.py'))
print(f'[sim] copied 联网_service.py only (NO protocol.py / eula.py / web_api.py / auto_update.py / version.py)')

# 列出临时目录内容
files = os.listdir(tmpdir)
print(f'[sim] temp dir contents: {files}')

print()
print('=' * 60)
print('[sim] 尝试从临时目录 import（模拟 NSSM 启动 Python）')
print('=' * 60)

# 用 subprocess 跑 Python，强制 cwd = 临时目录，sys.path[0] = 临时目录
code = '''
import sys
sys.path.insert(0, '.')
print(f'[subprocess] cwd={__import__("os").getcwd()}')
print(f'[subprocess] sys.path={sys.path[:3]}')
try:
    import 联网_service as main
    print('[subprocess] 联网_service import OK')
    print(f'[subprocess] VERSION = {main.VERSION}')
except Exception as e:
    print(f'[subprocess] 联网_service import FAIL: {type(e).__name__}: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
'''

result = subprocess.run(
    [sys.executable, '-c', code],
    cwd=tmpdir,
    capture_output=True,
    text=True,
    timeout=30,
)
print('STDOUT:')
print(result.stdout)
print('STDERR:')
print(result.stderr)
print(f'RETURNCODE: {result.returncode}')

# 清理
shutil.rmtree(tmpdir)
print(f'[sim] cleaned up {tmpdir}')
