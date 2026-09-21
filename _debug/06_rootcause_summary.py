# -*- coding: utf-8 -*-
"""
诊断脚本 06：根因汇总
- 对账 v2.0.2 解耦 commit 引入的所有新 .py 文件
- 列出 setup.iss 的 [Files] 段实际装的 .py 文件
- 输出根因结论
"""
import os, re

# v2.0.2 解耦 commit 引入的新 .py 文件
print('=' * 70)
print('STEP 1: v2.0.2 解耦 commit 引入的所有顶层 .py 模块')
print('=' * 70)

# 从 git log 解耦 commit 看
decoupling_modules = ['version.py', 'protocol.py', 'eula.py', 'web_api.py', 'auto_update.py']
for m in decoupling_modules:
    exists = os.path.exists(m)
    size = os.path.getsize(m) if exists else 0
    print(f'  [{("OK" if exists else "MISSING")}] {m:<22} ({size} bytes)')

# 联网_service.py 中的所有 import 语句
print()
print('=' * 70)
print('STEP 2: 联网_service.py 顶层 import 列表')
print('=' * 70)

src = open('联网_service.py', encoding='utf-8').read()
import ast as _ast
tree = _ast.parse(src)

imports = []
for node in tree.body:
    if isinstance(node, _ast.Import):
        for alias in node.names:
            imports.append(('import', alias.name, alias.asname))
    elif isinstance(node, _ast.ImportFrom):
        for alias in node.names:
            imports.append(('from', node.module, alias.name))

for kind, mod, name in imports:
    full = f'{mod}.{name}' if kind == 'from' else name
    if mod and mod.startswith(('protocol', 'eula', 'web_api', 'auto_update', 'version')):
        print(f'  {kind:<5} {mod:<15} -> {name}{(" as " + full.split(".")[-1]) if "as" in repr(mod) else ""}')

# setup.iss [Files] 段实际装的 .py 文件
print()
print('=' * 70)
print('STEP 3: setup.iss [Files] 段实际装的 .py 文件')
print('=' * 70)
with open('packaging/setup.iss', encoding='utf-8') as f:
    iss = f.read()

# 提取 [Files] 段
m = re.search(r'\[Files\](.*?)\[', iss, re.DOTALL)
if m:
    files_section = m.group(1)
    py_files = [line.strip() for line in files_section.split('\n') if line.strip().startswith('Source:') and '.py' in line]
    if py_files:
        for line in py_files:
            print(f'  {line}')
    else:
        print('  (none)')
else:
    print('  (no [Files] section)')

# 对账
print()
print('=' * 70)
print('STEP 4: 对账 — 解耦模块 vs 装包列表')
print('=' * 70)
expected = ['联网_service.py'] + decoupling_modules
packed = ['联网_service.py']  # 从 setup.iss 看只有这个
for m in decoupling_modules:
    in_setup = any(m in line for line in py_files) if py_files else False
    status = 'INSTALLED' if in_setup else 'NOT_INSTALLED'
    print(f'  [{status}] {m}')

print()
print('=' * 70)
print('ROOT CAUSE 结论')
print('=' * 70)
print()
print('  v2.0.2 启动失败的 root cause:')
print()
print('  packaging/setup.iss 第 42-49 行的 [Files] 段只装 联网_service.py')
print('  v2.0.2-decouple PR 引入的 5 个解耦模块（version.py / protocol.py /')
print('  eula.py / web_api.py / auto_update.py）完全没有 Source: 行')
print('  → 安装包只包含主入口，缺所有依赖 .py')
print()
print('  NSSM 启动命令（setup.iss line 200, 201-204）:')
print('    Application  = python.exe')
print('    AppParameters = "D:\\Program Files\\DrcomAutoLogin\\联网_service.py"')
print('    AppDirectory  = D:\\Program Files\\DrcomAutoLogin')
print()
print('  Python sys.path[0] = 联网_service.py 所在目录')
print('    = D:\\Program Files\\DrcomAutoLogin')
print()
print('  联网_service.py 顶层 import (line 40, 373, 374, 381, 387, 393):')
print('    from version import VERSION')
print('    from protocol import run_once')
print('    import protocol as _protocol_mod')
print('    import eula as _eula_mod')
print('    import web_api as _web_api_mod')
print('    import auto_update as _auto_update_mod')
print()
print('  → ModuleNotFoundError: No module named version (第一个 import 触发)')
print('  → Python 进程立即以 rc=1 退出')
print('  → NSSM 检测到进程退出 → 服务起不来')
