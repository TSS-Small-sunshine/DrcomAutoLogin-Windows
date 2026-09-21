# -*- coding: utf-8 -*-
"""
诊断脚本 01：globals 拓扑分析
- 检查关键 globals 是模块顶层定义还是 main() 内定义
- 关键问题：若某个 _attach() 需要的名字只在 main() 内赋值，
  但 _attach() 在 main() 调用前被 import 触发，则 NameError
"""
import sys
sys.path.insert(0, '.')
import ast as _ast

src = open('联网_service.py', encoding='utf-8').read()
tree = _ast.parse(src)

# 收集模块顶层赋值
top_assigns = {}
for node in tree.body:
    if isinstance(node, _ast.Assign):
        for t in node.targets:
            if isinstance(t, _ast.Name):
                top_assigns[t.id] = node.lineno
    elif isinstance(node, _ast.AnnAssign):
        if isinstance(node.target, _ast.Name):
            top_assigns[node.target.id] = node.lineno
    elif isinstance(node, _ast.FunctionDef):
        top_assigns[node.name] = node.lineno
    elif isinstance(node, _ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            top_assigns[name] = node.lineno
    elif isinstance(node, _ast.ImportFrom):
        for alias in node.names:
            name = alias.asname or alias.name
            top_assigns[name] = node.lineno

# main() 函数体内的赋值
main_func = None
for node in tree.body:
    if isinstance(node, _ast.FunctionDef) and node.name == 'main':
        main_func = node
        break

main_local_assigns = set()
for node in _ast.walk(main_func):
    if isinstance(node, _ast.Assign):
        for t in node.targets:
            if isinstance(t, _ast.Name):
                main_local_assigns.add(t.id)

critical = [
    'STATE', 'STATE_LOCK', 'BACKOFF', 'BACKOFF_LOCK', 'RUN_LOCK', 'STOP_EVENT', 'UPDATE_LOCK',
    'BASE_DIR', 'LOG_FILE', 'LOG_DIR', 'CONFIG_FILE', 'PASSWORD_FILE', 'UPGRADE_LOG_FILE', 'NSSM_PATH', 'TOOLS_DIR',
    '_PWD_VALUE', 'PWD_LOCK',
    'DEFAULT_CONFIG', 'ALLOWED_SUFFIXES', 'ALLOWED_INTERVALS', 'ALLOWED_UPDATE_INTERVALS',
    '_protocol_mod', '_eula_mod', '_web_api_mod', '_auto_update_mod',
    'logger',
]
print(f"{'NAME':<28} {'TOP_LEVEL_LINE':<14} {'IN_MAIN_LOCAL':<14} {'STATUS'}")
print('-' * 80)
for name in critical:
    top_line = top_assigns.get(name, 'NOT_AT_TOP_LEVEL')
    in_main = name in main_local_assigns
    status = 'TOP_LEVEL' if top_line != 'NOT_AT_TOP_LEVEL' else ('main_local_ONLY' if in_main else 'NEVER_DEFINED')
    print(f'{name:<28} {str(top_line):<14} {str(in_main):<14} {status}')
